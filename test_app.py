from quart import Quart, render_template, request, redirect, url_for, flash, session
import asyncpg
import asyncio
from config import DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, SECRET_KEY
import scripts.test_data_analysis
import logging
from asyncpg.exceptions import UniqueViolationError
from functools import wraps

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Quart(__name__)
app.secret_key = SECRET_KEY

# Создание пула соединений с базой данных при запуске приложения
@app.before_serving
async def startup():
    try:
        app.db_pool = await asyncpg.create_pool(
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT,
            min_size=1,
            max_size=10
        )
        logger.info("Пул соединений с базой данных успешно создан.")
    except Exception as e:
        logger.error(f"Ошибка при создании пула соединений: {e}")

@app.after_serving
async def shutdown():
    try:
        await app.db_pool.close()
        logger.info("Пул соединений с базой данных закрыт.")
    except Exception as e:
        logger.error(f"Ошибка при закрытии пула соединений: {e}")

# Маршрут главной страницы
@app.route("/")
async def index():
    return await render_template("index.html")

@app.route("/subscribe/<ticker>", methods=["POST"])
async def subscribe(ticker):
    if "user_id" not in session:
        await flash("Вы должны быть авторизованы для создания подписок.", "warning")
        return redirect(url_for("login"))

    user_id = session["user_id"]
    form = await request.form
    condition_type = form.get("condition_type")
    condition_value = form.get("condition_value")

    try:
        condition_value = float(condition_value)
    except ValueError:
        await flash("Некорректное значение условия.", "danger")
        return redirect(url_for("technical_analysis", ticker=ticker))

    async with app.db_pool.acquire() as conn:
        try:
            # Получаем asset_id для указанного тикера
            asset = await conn.fetchrow("SELECT asset_id FROM assets WHERE ticker = $1", ticker)
            if not asset:
                await flash(f"Актив с тикером {ticker} не найден.", "danger")
                return redirect(url_for("technical_analysis", ticker=ticker))

            asset_id = asset["asset_id"]

            # Вставляем новую подписку
            await conn.execute("""
                INSERT INTO subscriptions (user_id, asset_id, condition_type, condition_value, is_active)
                VALUES ($1, $2, $3, $4, TRUE)
            """, user_id, asset_id, condition_type, condition_value)

            await flash(f"Вы успешно подписались на актив {ticker} с условием {condition_type} {condition_value}.", "success")
            return redirect(url_for("technical_analysis", ticker=ticker))
        except Exception as e:
            logger.error(f"Ошибка при создании подписки: {e}")
            await flash("Произошла ошибка при создании подписки. Попробуйте позже.", "danger")
            return redirect(url_for("technical_analysis", ticker=ticker))


# Маршрут регистрации пользователя
@app.route("/register", methods=["GET", "POST"])
async def register():
    if request.method == "POST":
        form = await request.form
        name = form.get("name")
        email = form.get("email")
        password = form.get("password")

        # Валидация форм
        if not name or not email or not password:
            await flash("Все поля обязательны для заполнения.", "warning")
            return redirect(url_for("register"))

        # Регистрация пользователя через бэкенд-функцию
        user_id = await scripts.test_data_analysis.register_user(app.db_pool, name, password, email)
        if user_id:
            await flash("Регистрация успешна! Теперь вы можете войти.", "success")
            return redirect(url_for("login"))
        else:
            await flash("Пользователь с таким email уже существует или произошла ошибка.", "danger")
            return redirect(url_for("register"))
    return await render_template("register.html")

# Маршрут авторизации пользователя
@app.route("/login", methods=["GET", "POST"])
async def login():
    if request.method == "POST":
        form = await request.form
        email = form.get("email")
        password = form.get("password")

        # Валидация форм
        if not email or not password:
            await flash("Email и пароль обязательны для заполнения.", "warning")
            return redirect(url_for("login"))

        # Аутентификация пользователя через бэкенд-функцию
        is_authenticated = await scripts.test_data_analysis.authenticate_user(app.db_pool, email, password)
        if is_authenticated:
            async with app.db_pool.acquire() as conn:
                user = await conn.fetchrow("SELECT user_id FROM users WHERE email = $1", email)
                if user:
                    session["user_id"] = user["user_id"]
                    await flash("Вы успешно вошли в систему!", "success")
                    return redirect(url_for("dashboard"))
                else:
                    await flash("Пользователь не найден.", "danger")
                    return redirect(url_for("login"))
        else:
            await flash("Неверный email или пароль.", "danger")
            return redirect(url_for("login"))
    return await render_template("login.html")

@app.route("/logout")
async def logout():
    session.pop("user_id", None)
    await flash("Вы вышли из системы.", "info")
    return redirect(url_for("login"))

    
@app.route("/dashboard")
async def dashboard():
    if "user_id" not in session:
        await flash("Вы должны быть авторизованы для доступа к панели управления.", "warning")
        return redirect(url_for("login"))

    user_id = session["user_id"]

    async with app.db_pool.acquire() as conn:
        try:
            # Получение данных пользователя и его роли
            user = await conn.fetchrow("""
                SELECT u.name, u.email, r.name AS role
                FROM users u
                JOIN user_roles ur ON u.user_id = ur.user_id
                JOIN roles r ON ur.role_id = r.role_id
                WHERE u.user_id = $1
            """, user_id)

            if not user:
                await flash("Пользователь не найден.", "danger")
                return redirect(url_for("login"))

            user_name = user["name"]
            user_email = user["email"]
            user_role = user["role"]

            session["is_admin"] = (user_role == "Admin")

            # Получение уведомлений
            notifications = await conn.fetch("""
                SELECT notification_id, ticker, notification_type, message, status, created_at
                FROM notifications
                WHERE user_id = $1
                ORDER BY created_at DESC
            """, user_id)
            notifications_list = [
                {
                    "notification_id": row["notification_id"],
                    "ticker": row["ticker"],
                    "notification_type": row["notification_type"],
                    "message": row["message"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                }
                for row in notifications
            ]

            # Получение подписок
            subscriptions = await conn.fetch("""
                SELECT s.subscription_id, a.name AS asset_name, s.condition_type, s.condition_value, s.created_at
                FROM subscriptions s
                JOIN assets a ON s.asset_id = a.asset_id
                WHERE s.user_id = $1
                ORDER BY s.created_at DESC
            """, user_id)
            subscriptions_list = [
                {
                    "subscription_id": row["subscription_id"],
                    "asset_name": row["asset_name"],
                    "condition_type": row["condition_type"],
                    "condition_value": row["condition_value"],
                    "created_at": row["created_at"]
                }
                for row in subscriptions
            ]

            # Получение последних действий
            recent_actions = await scripts.test_data_analysis.get_recent_actions(app.db_pool, user_id)
            if recent_actions is None:
                recent_actions = []

        except Exception as e:
            await flash(f"Ошибка при загрузке данных пользователя: {e}", "danger")
            return redirect(url_for("login"))

    return await render_template(
        "dashboard.html",
        user_name=user_name,
        user_email=user_email,
        user_role=user_role, 
        notifications=notifications_list,
        subscriptions=subscriptions_list,
        recent_actions=recent_actions
    )



@app.route("/unsubscribe/<int:subscription_id>", methods=["POST"])
async def unsubscribe(subscription_id):
    if "user_id" not in session:
        await flash("Вы должны быть авторизованы для управления подписками.", "warning")
        return redirect(url_for("login"))

    async with app.db_pool.acquire() as conn:
        try:
            await conn.execute("""
                DELETE FROM subscriptions
                WHERE subscription_id = $1 AND user_id = $2
            """, subscription_id, session["user_id"])
            await flash("Подписка успешно удалена.", "success")
        except Exception as e:
            await flash(f"Ошибка при удалении подписки: {e}", "danger")
    
    return redirect(url_for("dashboard"))



# Маршрут создания уведомления
@app.route("/create_notification/<ticker>", methods=["POST"])
async def create_notification_route(ticker):
    if "user_id" not in session:
        await flash("Вы должны быть авторизованы для создания уведомлений.")
        return redirect(url_for("login"))

    user_id = session["user_id"]
    message = f"Уведомление для актива {ticker}: достигнут установленный порог."
    notification_type = "price_reached"

    try:
        async with app.db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO notifications (user_id, ticker, notification_type, message, status, created_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
            """, user_id, ticker, notification_type, message, 'pending')
        await flash(f"Уведомление для актива {ticker} успешно создано.", "success")
    except Exception as e:
        await flash(f"Ошибка при создании уведомления: {e}", "danger")

    return redirect(url_for("search"))

# Маршрут отметки уведомления как прочитанное
@app.route("/mark_notification/<notification_id>", methods=["POST"])
async def mark_notification_as_read(notification_id):
    try:
        result = await app.db_pool.execute("""
            UPDATE notifications
            SET status = 'read'
            WHERE notification_id = $1
        """, notification_id)
        if result.endswith("0"):
            await flash("Уведомление не найдено.", "warning")
        else:
            await flash("Уведомление отмечено как прочитанное.", "success")
    except Exception as e:
        await flash(f"Ошибка при обновлении уведомления: {e}", "danger")

    return redirect(url_for("dashboard"))

# Маршрут технического анализа
@app.route("/technical_analysis/<ticker>")
async def technical_analysis(ticker):
    async with app.db_pool.acquire() as conn:
        try:
            rows = await conn.fetch("""
                SELECT date, sma_20, rsi, macd, adx
                FROM technical_indicators
                WHERE ticker = $1
                ORDER BY date DESC
                LIMIT 30
            """, ticker)

            indicators = [
                {"date": row["date"], "sma_20": row["sma_20"], "rsi": row["rsi"], "macd": row["macd"], "adx": row["adx"]}
                for row in rows
            ]
        except Exception as e:
            await flash(f"Ошибка при получении технических индикаторов: {e}", "danger")
            indicators = []

    return await render_template("technical_analysis.html", ticker=ticker, indicators=indicators)

# Маршрут поиска тикеров
@app.route("/search", methods=["GET", "POST"])
async def search():
    results = []
    if request.method == "POST":
        form = await request.form
        query = form.get("query")
        if query:
            try:
                results = await scripts.test_data_analysis.search_ticker(app.db_pool, query)
            except Exception as e:
                await flash(f"Ошибка при поиске тикера: {e}", "danger")
        else:
            await flash("Введите запрос для поиска.", "warning")
    return await render_template("search.html", results=results)

# Маршрут построения графика
@app.route("/plot/<ticker>/<period>")
async def plot(ticker, period):
    if "user_id" in session:
        try:
            await scripts.test_data_analysis.record_user_action(app.db_pool, session["user_id"], ticker)
        except Exception as e:
            await flash(f"Ошибка при записи действия пользователя: {e}", "danger")

    try:
        graph_html = await scripts.test_data_analysis.plot_price_history(app.db_pool, ticker, period)
    except Exception as e:
        await flash(f"Ошибка при построении графика: {e}", "danger")
        graph_html = None

    if graph_html:
        return await render_template("plot.html", graph_html=graph_html, ticker=ticker, period=period)
    else:
        await flash("Нет данных для отображения.", "warning")
        return redirect(url_for("search"))

@app.route("/check_session")
async def check_session():
    user_id = session.get("user_id")
    if user_id:
        return f"User ID in session: {user_id}"
    else:
        return "No user in session."

def admin_required(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        if "user_id" not in session:
            logger.warning("Попытка доступа без авторизации.")
            await flash("Вы должны быть авторизованы для доступа к этой странице.", "warning")
            return redirect(url_for("login"))

        user_id = session["user_id"]
        async with app.db_pool.acquire() as conn:
            try:
                role = await conn.fetchval("""
                    SELECT r.name
                    FROM user_roles ur
                    JOIN roles r ON ur.role_id = r.role_id
                    WHERE ur.user_id = $1;
                """, user_id)

                if role != "Administrator":
                    logger.warning(f"Доступ запрещен для пользователя {user_id} с ролью {role}.")
                    await flash("У вас нет прав для доступа к этой странице.", "danger")
                    return redirect(url_for("dashboard"))
            except Exception as e:
                logger.error(f"Ошибка при проверке роли пользователя {user_id}: {e}")
                await flash("Произошла ошибка при проверке ваших прав.", "danger")
                return redirect(url_for("dashboard"))

        return await func(*args, **kwargs)
    return wrapped



@app.route("/admin", methods=["GET", "POST"])
@admin_required
async def admin_panel():
    message = None
    if request.method == "POST":
        form = await request.form
        action = form.get("action")
        target_user_id = form.get("user_id")

        if action == "make_admin":
            message = await scripts.test_data_analysis.make_admin(app.db_pool, int(target_user_id))

        elif action == "backup_db":
            message = await scripts.test_data_analysis.backup_database()

        elif action == "recalculate":
            message = await scripts.test_data_analysis.recalculate_indicators(app.db_pool)

        if message:
            await flash(message, "success" if "успешно" in message else "danger")

    users = await scripts.test_data_analysis.get_users(app.db_pool)

    return await render_template("admin.html", users=users)

if __name__ == "__main__":
    async def main():
        # Ждем создания пула перед запуском фоновой задачи
        await startup()
        asyncio.create_task(scripts.test_data_analysis.check_subscriptions(app.db_pool))
        logger.info("Фоновая задача для проверки подписок запущена.")
        await app.run_task()

    asyncio.run(main())