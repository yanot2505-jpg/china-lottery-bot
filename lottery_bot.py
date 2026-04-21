import asyncio
import logging
import os
import random

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import CallbackQuery
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в .env")

if not ADMIN_ID_RAW.isdigit():
    raise ValueError("ADMIN_ID в .env должен быть числом")

ADMIN_ID = int(ADMIN_ID_RAW)
DB_PATH = "lottery_bot.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())


class LotteryForm(StatesGroup):
    waiting_for_fio = State()


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS participants (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT NOT NULL,
                lottery_number INTEGER NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def get_participant(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT user_id, username, full_name, lottery_number, created_at
            FROM participants
            WHERE user_id = ?
        """, (user_id,))
        row = await cursor.fetchone()

    if not row:
        return None

    return {
        "user_id": row[0],
        "username": row[1],
        "full_name": row[2],
        "lottery_number": row[3],
        "created_at": row[4],
    }


async def get_used_numbers() -> set[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT lottery_number FROM participants")
        rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def assign_unique_number():
    used_numbers = await get_used_numbers()
    all_numbers = set(range(1, 401))
    available_numbers = list(all_numbers - used_numbers)

    if not available_numbers:
        return None

    return random.choice(available_numbers)


async def save_participant(user_id: int, username: str | None, full_name: str, lottery_number: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO participants (user_id, username, full_name, lottery_number)
            VALUES (?, ?, ?, ?)
        """, (user_id, username, full_name, lottery_number))
        await db.commit()


async def get_all_participants():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT user_id, username, full_name, lottery_number, created_at
            FROM participants
            ORDER BY lottery_number ASC
        """)
        rows = await cursor.fetchall()

    result = []
    for row in rows:
        result.append({
            "user_id": row[0],
            "username": row[1],
            "full_name": row[2],
            "lottery_number": row[3],
            "created_at": row[4],
        })
    return result


def normalize_fio(text: str) -> str:
    return " ".join((text or "").strip().split())


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    participant = await get_participant(message.from_user.id)

    if participant:
        username_text = f"@{participant['username']}" if participant["username"] else "без username"
        await message.answer(
            "Вы уже участвуете в лотерее.\n\n"
            f"ФИО: <b>{participant['full_name']}</b>\n"
            f"Username: <b>{username_text}</b>\n"
            f"Ваш номер: <b>{participant['lottery_number']}</b>"
        )
        return

    await state.set_state(LotteryForm.waiting_for_fio)
    await message.answer("Введите ФИО")


@dp.message(LotteryForm.waiting_for_fio)
async def process_fio(message: Message, state: FSMContext) -> None:
    fio = normalize_fio(message.text)

    if len(fio) < 5:
        await message.answer("Введите ФИО полностью")
        return

    existing_participant = await get_participant(message.from_user.id)
    if existing_participant:
        await state.clear()
        await message.answer(
            f"Вы уже участвуете в лотерее.\nВаш номер: <b>{existing_participant['lottery_number']}</b>"
        )
        return

    try:
        number = await assign_unique_number()
        if number is None:
            await state.clear()
            await message.answer("Свободные номера закончились.")
            return

        await save_participant(
            user_id=message.from_user.id,
            username=message.from_user.username,
            full_name=fio,
            lottery_number=number
        )

        await state.clear()

        await message.answer(
            f"Гость участвует в лотерее! Ваш номер: <b>{number}</b>"
        )

        username_text = f"@{message.from_user.username}" if message.from_user.username else "без username"

        try:
            await bot.send_message(
                ADMIN_ID,
                "Новый участник лотереи:\n\n"
                f"ФИО: <b>{fio}</b>\n"
                f"Username: <b>{username_text}</b>\n"
                f"Telegram ID: <code>{message.from_user.id}</code>\n"
                f"Номер: <b>{number}</b>"
            )
        except Exception as exc:
            logger.warning("Не удалось отправить админу уведомление: %s", exc)

    except Exception as exc:
        logger.exception("Ошибка при регистрации участника: %s", exc)
        await state.clear()
        await message.answer("Уважаемая гость, интернета не работает, попробуй еще раз")


@dp.message(Command("participants"))
async def cmd_participants(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return

    participants = await get_all_participants()
    if not participants:
        await message.answer("Пока участников нет.")
        return

    chunks = []
    current_chunk = "Список участников:\n\n"

    for index, participant in enumerate(participants, start=1):
        username_text = f"@{participant['username']}" if participant["username"] else "без username"
        line = (
            f"{index}. {participant['full_name']} | "
            f"{username_text} | "
            f"ID: {participant['user_id']} | "
            f"№ {participant['lottery_number']}\n"
        )

        if len(current_chunk) + len(line) > 3500:
            chunks.append(current_chunk)
            current_chunk = ""

        current_chunk += line

    if current_chunk:
        chunks.append(current_chunk)

    for chunk in chunks:
        await message.answer(chunk)


@dp.message(Command("count"))
async def cmd_count(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return

    participants = await get_all_participants()
    await message.answer(
        f"Всего участников: <b>{len(participants)}</b>\n"
        f"Свободных номеров: <b>{400 - len(participants)}</b>"
    )
def admin_draw_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Сгенерировать числа", callback_data="draw_winners")]
        ]
    )


def reroll_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Сгенерировать заново", callback_data="draw_winners")]
        ]
    )


async def get_random_winners(count: int = 3) -> list[dict]:
    participants = await get_all_participants()

    if not participants:
        return []

    if len(participants) <= count:
        return random.sample(participants, len(participants))

    return random.sample(participants, count)


def format_winners_message(winners: list[dict]) -> str:
    if not winners:
        return "В лотерее пока нет участников."

    lines = ["🎉 <b>Результат генерации</b>\n"]

    for index, participant in enumerate(winners, start=1):
        username = f"@{participant['username']}" if participant["username"] else "без username"

        lines.append(
            f"<b>{index}.</b> {participant['full_name']}\n"
            f"Username: {username}\n"
            f"Номер: <b>{participant['lottery_number']}</b>\n"
        )

    return "\n".join(lines)


@dp.message(Command("draw"))
async def cmd_draw(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return

    await message.answer(
        "Панель розыгрыша.\nНажми кнопку ниже:",
        reply_markup=admin_draw_keyboard()
    )


@dp.callback_query(lambda c: c.data == "draw_winners")
async def process_draw_winners(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Недоступно", show_alert=True)
        return

    winners = await get_random_winners(3)
    text = format_winners_message(winners)

    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=reroll_keyboard()
        )

    await callback.answer("Готово")

async def main() -> None:
    await init_db()
    print("Lottery bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
    
