import asyncpg
import aiohttp
import pandas as pd
import pandas_ta as ta
import json
import os
from datetime import datetime, timedelta
import plotly.graph_objects as go
import asyncio
import bcrypt
from asyncpg.exceptions import UniqueViolationError
import logging
from quart import current_app
from config import DB_USER, DB_NAME, DB_HOST, DB_PORT
import requests


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

formatter = logging.Formatter('[%(asctime)s] %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)

if not logger.handlers:
    logger.addHandler(console_handler)



async def hash_password(password: str) -> str:
    try:
        loop = asyncio.get_running_loop()
        hashed = await loop.run_in_executor(
            None,
            bcrypt.hashpw,
            password.encode('utf-8'),
            bcrypt.gensalt()
        )
        hashed_str = hashed.decode('utf-8')
        logger.debug(f"Пароль успешно захеширован: {hashed_str}")
        return hashed_str
    except Exception as e:
        logger.error(f"Ошибка при хешировании пароля: {e}")
        raise


async def check_password(password: str, hashed: str) -> bool:
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            bcrypt.checkpw,
            password.encode('utf-8'),
            hashed.encode('utf-8')
        )
        logger.debug(f"Проверка пароля {'успешна' if result else 'неудачна'}.")
        return result
    except Exception as e:
        logger.error(f"Ошибка при проверке пароля: {e}")
        raise


# Функция для регистрации пользователя
async def register_user(pool: asyncpg.pool.Pool, name: str, password: str, email: str):
    try:
        logger.info(f"Начинается регистрация пользователя: {email}")
        password_hash = await hash_password(password)

        async with pool.acquire() as conn:
            async with conn.transaction():
                user_id = await conn.fetchval("""
                    INSERT INTO users (name, password_hash, email, registration_date)
                    VALUES ($1, $2, $3, NOW())
                    RETURNING user_id;
                """, name, password_hash, email)

                # Назначение роли по умолчанию
                await conn.execute("""
                    INSERT INTO user_roles (user_id, role_id)
                    VALUES ($1, (SELECT role_id FROM roles WHERE name = 'User'))
                """, user_id)

        logger.info(f"Пользователь {email} успешно зарегистрирован с ID: {user_id}")
        return user_id

    except asyncpg.exceptions.UniqueViolationError:
        logger.warning(f"Пользователь с email {email} уже существует.")
        return None

    except Exception as e:
        logger.error(f"Ошибка при регистрации пользователя {email}: {e}")

        # Удаляем возможные невалидные записи, если они были созданы
        async with pool.acquire() as conn:
            await conn.execute("""
                DELETE FROM users WHERE email = $1
            """, email)

        return None

# Функция для аутентификации пользователя
async def authenticate_user(pool: asyncpg.pool.Pool, email: str, password: str) -> bool:
    try:
        logger.info(f"Пытаемся аутентифицировать пользователя: {email}")
        async with pool.acquire() as conn:
            result = await conn.fetchrow("""
                SELECT password_hash FROM users WHERE email = $1;
            """, email)
        if result is None:
            logger.warning(f"Пользователь с email {email} не найден.")
            return False
        password_hash = result["password_hash"]
        if await check_password(password, password_hash):
            logger.info(f"Аутентификация пользователя {email} успешна.")
            return True
        else:
            logger.warning(f"Неверный пароль для пользователя {email}.")
            return False
    except Exception as e:
        logger.error(f"Ошибка при аутентификации пользователя {email}: {e}")
        return False


# Функция для поиска тикера по названию актива
async def search_ticker(pool: asyncpg.pool.Pool, query: str):
    try:
        logger.info(f"Ищем тикеры по запросу: {query}")
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT ticker, name FROM assets
                WHERE name ILIKE $1
                LIMIT 5
            """, f"%{query}%")
            results = [{"ticker": row["ticker"], "name": row["name"]} for row in rows]
            logger.info(f"Найдено {len(results)} тикеров по запросу '{query}'.")
            return results
    except Exception as e:
        logger.error(f"Ошибка в search_ticker для запроса '{query}': {e}")
        return []


# Функция для записи действия пользователя
async def record_user_action(pool: asyncpg.pool.Pool, user_id: int, ticker: str):
    try:
        logger.info(f"Запись действия пользователя {user_id} по тикеру {ticker}.")
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_actions (user_id, ticker, action_time)
                VALUES ($1, $2, NOW())
            """, user_id, ticker)
        logger.info(f"Действие пользователя {user_id} по тикеру {ticker} успешно записано.")
    except Exception as e:
        logger.error(f"Ошибка при записи действия пользователя {user_id} по тикеру {ticker}: {e}")

async def get_recent_actions(pool: asyncpg.pool.Pool, user_id: int, limit: int = 5):
    try:
        logger.info(f"Получение последних действий пользователя user_id={user_id} с ограничением limit={limit}")
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT ticker, MAX(action_time) AS last_action
                FROM user_actions
                WHERE user_id = $1
                GROUP BY ticker
                ORDER BY last_action DESC
                LIMIT $2
            """, user_id, limit)
        
        if not rows:
            logger.warning(f"Последние действия для пользователя user_id={user_id} отсутствуют.")
            return []

        logger.info(f"Получено {len(rows)} записей последних действий для пользователя user_id={user_id}.")
        return [{"ticker": row["ticker"], "last_action": row["last_action"]} for row in rows]
    
    except Exception as e:
        logger.error(f"Ошибка получения последних действий для пользователя user_id={user_id}: {e}")
        return []


# Функция для расчета технических индикаторов
async def calculate_technical_indicators(pool: asyncpg.pool.Pool, ticker: str, historical_data: list):
    try:
        logger.info(f"Расчет технических индикаторов для {ticker}.")
        df = pd.DataFrame(historical_data)
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)

        # Расчет индикаторов
        df['SMA_20'] = ta.sma(df['close'], length=20)  
        df['RSI'] = ta.rsi(df['close'])  
        macd = ta.macd(df['close'])
        df['MACD'] = macd['MACD_12_26_9']
        df['ADX'] = ta.adx(df['high'], df['low'], df['close'])['ADX_14']

        async with pool.acquire() as conn:
            async with conn.transaction():
                for date, row in df.iterrows():
                    await conn.execute("""
                        INSERT INTO technical_indicators (ticker, date, sma_20, rsi, macd, adx)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (ticker, date) DO UPDATE
                        SET sma_20 = EXCLUDED.sma_20,
                            rsi = EXCLUDED.rsi,
                            macd = EXCLUDED.macd,
                            adx = EXCLUDED.adx
                    """, ticker, date, row['SMA_20'], row['RSI'], row['MACD'], row['ADX'])
        logger.info(f"Технические индикаторы для {ticker} успешно рассчитаны и сохранены.")
        return df  
    except Exception as e:
        logger.error(f"Ошибка при расчете технических индикаторов для {ticker}: {e}")
        return pd.DataFrame()


# Функции для работы с кэшем
async def fetch_data_from_cache(pool: asyncpg.pool.Pool, ticker: str, period: str):
    try:
        logger.info(f"Проверка кэша для {ticker} за период {period}.")
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT data FROM valid_cached_data
                WHERE ticker = $1 AND period = $2
            """, ticker, period)
            if row:
                logger.info(f"Данные для {ticker} за период {period} найдены в кэше.")
                return json.loads(row["data"])
            else:
                logger.info(f"Данные для {ticker} за период {period} не найдены в кэше.")
        return None
    except Exception as e:
        logger.error(f"Ошибка при получении данных из кэша для {ticker} за период {period}: {e}")
        return None



async def save_data_to_cache(pool: asyncpg.pool.Pool, ticker: str, period: str, data: list):
    try:
        logger.info(f"Сохранение данных для {ticker} за период {period} в кэш.")
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO cached_data (ticker, period, data, last_updated)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (ticker, period) DO UPDATE
                SET data = EXCLUDED.data,
                    last_updated = NOW()
            """, ticker, period, json.dumps(data))
        logger.info(f"Данные для {ticker} за период {period} успешно сохранены в кэш.")
    except Exception as e:
        logger.error(f"Ошибка при сохранении данных в кэш для {ticker} за период {period}: {e}")


# Функция для получения исторических данных
async def fetch_historical_data(pool: asyncpg.pool.Pool, ticker: str, period: str = "1d"):
    try:
        # Проверка кэша для периодов больше одного дня
        if period != "1d":
            cached_data = await fetch_data_from_cache(pool, ticker, period)
            if cached_data:
                return cached_data

        logger.info(f"Запрос данных для {ticker} за период {period}.")
        base_url = f"https://iss.moex.com/iss/engines/stock/markets/shares/securities/{ticker}/candles.json"
        
        days_map = {
            "1d": 1,
            "5d": 5,
            "1m": 30,
            "3m": 90,
            "1y": 365
        }
        interval_map = {
            "1d": 1,
            "5d": 10,
            "1m": 60,
            "3m": 60,
            "1y": 24
        }
        
        days = days_map.get(period, 1)
        interval = interval_map.get(period, 24)
        from_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

        params = {
            "from": from_date,
            "interval": interval
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(base_url, params=params) as response:
                if response.status == 200:
                    logger.info(f"Успешный запрос к API для {ticker}. Обработка данных...")
                    data = await response.json()
                    candles = data.get("candles", {}).get("data", [])
                    
                    transformed_data = []
                    for item in candles:
                        try:
                            date = datetime.strptime(item[6], "%Y-%m-%d %H:%M:%S") 
                            open_price = item[0]  
                           
                            transformed_data.append({"date": date.isoformat(), "open": open_price})
          
                        except (ValueError, IndexError) as e:
                            logger.error(f"Ошибка обработки записи {item}: {e}")
                    
                    if period != "1d":
                        await save_data_to_cache(pool, ticker, period, transformed_data)
    
                    logger.info(f"Данные для {ticker} за период {period} успешно получены и обработаны.")
                    return transformed_data
                else:
                    logger.error(f"Ошибка при запросе данных с API для {ticker}: статус {response.status}")
                    return None
    except Exception as e:
        logger.exception(f"Произошла ошибка в функции fetch_historical_data для {ticker} за период {period}: {e}")
        return None

async def plot_price_history(pool: asyncpg.pool.Pool, ticker: str, period: str = "1d"):
    try:
        print(f"[{datetime.now()}] Построение графика для {ticker} за период {period}")
        
        # Получение данных
        data = await fetch_historical_data(pool, ticker, period)
        if not data:
            print(f"[{datetime.now()}] Нет данных для отображения графика для {ticker}")
            return None

        dates = [datetime.fromisoformat(entry["date"]) for entry in data]
        open_prices = [entry["open"] for entry in data]

        # Построение и настройка графика 
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=dates,
            y=open_prices,
            mode='lines',
            line=dict(color='black', width=1)
        ))

        fig.update_layout(
            title=f"График цены открытия {ticker} за период {period}",
            xaxis_title="Дата",
            yaxis_title="Цена",
            hovermode="x unified",
            template="plotly_white",
            autosize=True
        )

        print(f"[{datetime.now()}] График для {ticker} за период {period} успешно построен.")
        
        return fig.to_html(full_html=False)
    except Exception as e:
        print(f"[{datetime.now()}] Ошибка при построении графика для {ticker} за период {period}: {e}")
        return None

async def check_subscriptions(db_pool):
    while True:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("SELECT process_subscriptions();")
                logger.info("Успешно обработаны подписки.")
        except Exception as e:
            logger.error(f"Ошибка при обработке подписок: {e}")
        
        await asyncio.sleep(60)


async def make_admin(conn, user_id):
    try:
        user_id = int(user_id)  
        await conn.execute("""
            UPDATE user_roles
            SET role_id = (SELECT role_id FROM roles WHERE name = 'Administrator')
            WHERE user_id = $1
        """, user_id)
        logger.info(f"Пользователь с ID {user_id} успешно назначен администратором.")
    except ValueError:
        logger.error(f"Переданный user_id не является числом: {user_id}")
        raise ValueError("user_id должен быть целым числом.")
    except DataError as e:
        logger.error(f"Ошибка данных при назначении администратора: {e}")
        raise
    except Exception as e:
        logger.error(f"Ошибка при назначении администратора: {e}")
        raise

async def backup_database():
    try:
        backup_filename = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql"
        command = f"pg_dump -U {DB_USER} -h {DB_HOST} {DB_NAME} > {backup_filename}"
        os.system(command)
        logger.info(f"База данных успешно сохранена в файл {backup_filename}.")
        return f"База данных успешно сохранена в файл {backup_filename}."
    except Exception as e:
        logger.error(f"Ошибка при резервном копировании базы данных: {e}")
        return f"Ошибка при резервном копировании базы данных: {e}"



async def fetch_moex_data(ticker: str):
    try:
        base_url = f"https://iss.moex.com/iss/engines/stock/markets/shares/securities/{ticker}/candles.json"
        from_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')  
        params = {
            "from": from_date,
            "interval": 24  
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(base_url, params=params) as response:
                if response.status != 200:
                    logger.error(f"Ошибка запроса: статус {response.status} для {ticker}.")
                    return None
                data = await response.json()
                candles = data.get("candles", {}).get("data")
                columns = data.get("candles", {}).get("columns")

                if not candles or not columns:
                    logger.warning(f"Нет данных для тикера {ticker}.")
                    return None

                df = pd.DataFrame(candles, columns=columns)
                if df.empty:
                    logger.warning(f"Пустой DataFrame для тикера {ticker}.")
                    return None

                # Конвертируем дату в datetime и упрощаем столбцы
                df["begin"] = pd.to_datetime(df["begin"])
                df.rename(columns={"open": "open", "close": "close", "high": "high", "low": "low"}, inplace=True)

                logger.info(f"Успешно получены данные для {ticker}.")
                return df

    except Exception as e:
        logger.error(f"Ошибка при получении данных для {ticker}: {e}")
        return None

# Асинхронная функция для расчета технических индикаторов
async def calculate_technical_indicators(pool: asyncpg.pool.Pool, ticker: str):
    try:
        logger.info(f"Начинается расчет технических индикаторов для {ticker}.")

        df = await fetch_moex_data(ticker)
        if df is None or df.empty:
            logger.warning(f"Нет данных для {ticker}. Пропускаем расчет.")
            return

        required_columns = {"close", "high", "low"}
        if not required_columns.issubset(df.columns):
            logger.error(f"Недостаточно данных для расчета индикаторов для {ticker}. Отсутствуют необходимые столбцы.")
            return

        # Преобразуем данные в числовой формат
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df['high'] = pd.to_numeric(df['high'], errors='coerce')
        df['low'] = pd.to_numeric(df['low'], errors='coerce')

        # Удаляем строки с пропущенными значениями
        df.dropna(subset=['close', 'high', 'low'], inplace=True)

        if df.empty:
            logger.warning(f"После удаления пропущенных значений данные для {ticker} пусты. Пропускаем расчет.")
            return

        # Установка индекса и сортировка по дате
        df.set_index("begin", inplace=True)
        df.sort_index(inplace=True)

        sma_period = 5
        rsi_period = 5
        macd_fast = 3
        macd_slow = 6
        macd_signal = 3
        adx_period = 5

        min_data_points = max(sma_period, rsi_period, macd_slow, adx_period)
        if len(df) < min_data_points:
            logger.warning(f"Недостаточно данных для расчета индикаторов для {ticker}. Требуется минимум {min_data_points} точек данных.")
            return

        # Расчет индикаторов
        logger.info(f"Расчет технических индикаторов для {ticker}.")
        df["sma"] = ta.sma(df["close"], length=sma_period)
        df["rsi"] = ta.rsi(df["close"], length=rsi_period)

        macd = ta.macd(df["close"], fast=macd_fast, slow=macd_slow, signal=macd_signal)
        if macd is not None and not macd.empty:
            macd_column = [col for col in macd.columns if 'MACD_' in col and 'signal' not in col and 'hist' not in col]
            if macd_column:
                df["macd"] = macd[macd_column[0]]
            else:
                logger.error(f"MACD столбец не найден для {ticker}.")
                df["macd"] = None
        else:
            logger.error(f"Ошибка расчета MACD для {ticker}.")
            df["macd"] = None

        adx = ta.adx(df["high"], df["low"], df["close"], length=adx_period)
        if adx is not None and not adx.empty:
            adx_column = [col for col in adx.columns if 'ADX_' in col]
            if adx_column:
                df["adx"] = adx[adx_column[0]]
            else:
                logger.error(f"ADX столбец не найден для {ticker}.")
                df["adx"] = None
        else:
            logger.error(f"Ошибка расчета ADX для {ticker}.")
            df["adx"] = None

        df.dropna(subset=["sma", "rsi", "macd", "adx"], inplace=True)

        if df.empty:
            logger.warning(f"Недостаточно данных для расчета индикаторов для {ticker}. Пропускаем.")
            return

        logger.info(f"Сохранение технических индикаторов для {ticker} в базе данных.")
        async with pool.acquire() as conn:
            async with conn.transaction():
                for date, row in df.iterrows():
                    await conn.execute("""
                        INSERT INTO technical_indicators (ticker, date, sma, rsi, macd, adx)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (ticker, date) DO UPDATE
                        SET sma = EXCLUDED.sma, 
                            rsi = EXCLUDED.rsi,
                            macd = EXCLUDED.macd, 
                            adx = EXCLUDED.adx
                    """, ticker, date, row["sma"], row["rsi"], row["macd"], row["adx"])

        logger.info(f"Технические индикаторы для {ticker} успешно рассчитаны и сохранены.")
    except Exception as e:
        logger.error(f"Ошибка при расчете технических индикаторов для {ticker}: {e}")

async def recalculate_indicators(pool):
    async with pool.acquire() as conn:
        tickers = await conn.fetch("SELECT ticker FROM assets")

    for ticker_row in tickers:
        ticker = ticker_row["ticker"]
        await calculate_technical_indicators(pool, ticker)


async def get_users(pool):
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT u.user_id, u.name, u.email, r.name AS role
            FROM users u
            JOIN user_roles ur ON u.user_id = ur.user_id
            JOIN roles r ON ur.role_id = r.role_id
            ORDER BY u.user_id
        """)
