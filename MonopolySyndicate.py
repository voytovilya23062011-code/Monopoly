import asyncio
import logging
import os
import time
import re
import random
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import asyncpg
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.enums import ChatType
from aiogram.filters import CommandStart, CommandObject
from aiogram.client.session.aiohttp import AiohttpSession

# ======================== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ========================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден! Добавьте переменную на Railway.")
if not DATABASE_URL:
    raise ValueError("❌ DATABASE_URL не найден! Добавьте переменную на Railway.")

# ======================== БАЗА ДАННЫХ (POSTGRESQL) ========================
class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10)

    async def init_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    capital INTEGER DEFAULT 500,
                    last_collect INTEGER DEFAULT 0
                )
            """)

    async def get_player(self, user_id: int) -> Optional[Dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id, username, capital, last_collect FROM players WHERE user_id = $1",
                user_id
            )
            return dict(row) if row else None

    async def create_player(self, user_id: int, username: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO players (user_id, username, capital) VALUES ($1, $2, $3) ON CONFLICT (user_id) DO NOTHING",
                user_id, username or f"ID_{user_id}", 500
            )

    async def update_capital(self, user_id: int, amount: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE players SET capital = capital + $1 WHERE user_id = $2",
                amount, user_id
            )

    async def update_collect_time(self, user_id: int, t: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE players SET last_collect = $1 WHERE user_id = $2",
                t, user_id
            )

    async def get_user_by_username(self, username: str) -> Optional[Dict]:
        if username.startswith('@'):
            username = username[1:]
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id, username, capital, last_collect FROM players WHERE LOWER(username) = LOWER($1)",
                username
            )
            return dict(row) if row else None

db = Database(DATABASE_URL)

# ======================== КОНФИГ ========================
COOLDOWN_SECONDS = 300
INCOME_AMOUNT = 500
TAKEOVER_PERCENT = 0.20

# Покер
ANTE = 100
MIN_PLAYERS = 2
MAX_PLAYERS = 6

# ======================== ИНИЦИАЛИЗАЦИЯ БОТА ========================
session = AiohttpSession(timeout=60)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# ======================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ========================
def escape_md(text: str) -> str:
    if not text:
        return ""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', str(text))

# ======================== ПОКЕР: ЛОГИКА КАРТ ========================
RANKS = '23456789TJQKA'
SUITS = '♠♥♦♣'

def new_deck() -> List[str]:
    return [r+s for r in RANKS for s in SUITS]

def shuffle_deck(deck: List[str]) -> List[str]:
    random.shuffle(deck)
    return deck

def deal_cards(deck: List[str], n: int) -> Tuple[List[str], List[str]]:
    hand = deck[:n]
    rest = deck[n:]
    return hand, rest

def card_value(card: str) -> int:
    return RANKS.index(card[0]) + 2

def hand_rank(hand: List[str], community: List[str]) -> Tuple[int, List[int]]:
    all_cards = hand + community
    if len(all_cards) < 5:
        return 0, sorted([card_value(c) for c in hand], reverse=True)
    values = sorted([card_value(c) for c in all_cards], reverse=True)
    suits = [c[1] for c in all_cards]
    is_flush = any(suits.count(s) >= 5 for s in SUITS)
    unique_vals = sorted(set(values), reverse=True)
    straight = False
    straight_high = 0
    if len(unique_vals) >= 5:
        for i in range(len(unique_vals)-4):
            if unique_vals[i] - unique_vals[i+4] == 4:
                straight = True
                straight_high = unique_vals[i]
                break
        if not straight and set([14,2,3,4,5]).issubset(set(values)):
            straight = True
            straight_high = 5
    if is_flush and straight:
        flush_suit = [s for s in SUITS if suits.count(s) >= 5][0]
        flush_vals = sorted([card_value(c) for c in all_cards if c[1] == flush_suit], reverse=True)
        if set([10,11,12,13,14]).issubset(set(flush_vals)):
            return 9, [14]
        for i in range(len(flush_vals)-4):
            if flush_vals[i] - flush_vals[i+4] == 4:
                return 8, [flush_vals[i]]
        if set([14,2,3,4,5]).issubset(set(flush_vals)):
            return 8, [5]
    val_count = defaultdict(int)
    for v in values:
        val_count[v] += 1
    counts = sorted(val_count.items(), key=lambda x: (-x[1], -x[0]))
    if counts[0][1] == 4:
        return 7, [counts[0][0]] + sorted([v for v, c in val_count.items() if c != 4], reverse=True)[:1]
    if counts[0][1] == 3 and len(counts) > 1 and counts[1][1] >= 2:
        return 6, [counts[0][0], counts[1][0]]
    if is_flush:
        flush_vals = sorted([card_value(c) for c in all_cards if c[1] == flush_suit], reverse=True)
        return 5, flush_vals[:5]
    if straight:
        return 4, [straight_high]
    if counts[0][1] == 3:
        return 3, [counts[0][0]] + sorted([v for v, c in val_count.items() if c != 3], reverse=True)[:2]
    if len(counts) >= 2 and counts[0][1] == 2 and counts[1][1] == 2:
        return 2, sorted([counts[0][0], counts[1][0]], reverse=True) + [counts[2][0]]
    if counts[0][1] == 2:
        return 1, [counts[0][0]] + sorted([v for v, c in val_count.items() if c != 2], reverse=True)[:3]
    return 0, values[:5]

def compare_hands(hand1: List[str], comm1: List[str], hand2: List[str], comm2: List[str]) -> int:
    rank1, k1 = hand_rank(hand1, comm1)
    rank2, k2 = hand_rank(hand2, comm2)
    if rank1 > rank2: return 1
    if rank1 < rank2: return -1
    for a,b in zip(k1,k2):
        if a > b: return 1
        if a < b: return -1
    return 0

# ======================== ПОКЕР: СОСТОЯНИЕ ИГРЫ ========================
class PokerGame:
    def __init__(self, chat_id: int, creator_id: int):
        self.chat_id = chat_id
        self.creator_id = creator_id
        self.players: List[int] = []
        self.player_data: Dict[int, Dict] = {}
        self.deck: List[str] = []
        self.community: List[str] = []
        self.pot = 0
        self.current_bet = 0
        self.stage = 'waiting'
        self.current_player_index = 0
        self.last_raiser = None
        self.round_bets = {}
        self.started = False

    def add_player(self, user_id: int) -> bool:
        if user_id in self.players:
            return False
        if len(self.players) >= MAX_PLAYERS:
            return False
        self.players.append(user_id)
        self.player_data[user_id] = {'hand': [], 'folded': False, 'bet': 0}
        return True

    def remove_player(self, user_id: int):
        if user_id in self.players:
            self.players.remove(user_id)
            self.player_data.pop(user_id, None)

    def start_game(self):
        if len(self.players) < MIN_PLAYERS:
            return False
        self.started = True
        self.stage = 'preflop'
        self.deck = shuffle_deck(new_deck())
        for uid in self.players:
            hand, self.deck = deal_cards(self.deck, 2)
            self.player_data[uid]['hand'] = hand
            self.player_data[uid]['folded'] = False
            self.player_data[uid]['bet'] = 0
        self.community = []
        self.pot = 0
        self.current_bet = 0
        self.last_raiser = None
        self.round_bets = {uid: 0 for uid in self.players}
        self.current_player_index = 0
        for uid in self.players:
            self._place_bet(uid, ANTE)
        self.pot = ANTE * len(self.players)
        self.current_bet = ANTE
        self._advance_stage()
        return True

    async def _place_bet(self, uid: int, amount: int):
        if uid not in self.player_data:
            return
        self.player_data[uid]['bet'] += amount
        self.round_bets[uid] += amount
        self.pot += amount
        await db.update_capital(uid, -amount)

    def _advance_stage(self):
        if self.stage == 'preflop':
            self.stage = 'flop'
            self.community, self.deck = deal_cards(self.deck, 3)
        elif self.stage == 'flop':
            self.stage = 'turn'
            self.community, self.deck = deal_cards(self.deck, 1)
        elif self.stage == 'turn':
            self.stage = 'river'
            self.community, self.deck = deal_cards(self.deck, 1)
        elif self.stage == 'river':
            self.stage = 'showdown'
        self.current_bet = 0
        self.last_raiser = None
        self.round_bets = {uid: 0 for uid in self.players}
        self.current_player_index = self._next_alive_player(-1)

    def _next_alive_player(self, start_index: int) -> int:
        n = len(self.players)
        for i in range(1, n+1):
            idx = (start_index + i) % n
            uid = self.players[idx]
            if not self.player_data[uid]['folded']:
                return idx
        return -1

    async def player_action(self, uid: int, action: str, amount: int = 0) -> bool:
        if uid not in self.players or self.player_data[uid]['folded']:
            return False
        if self.stage == 'showdown':
            return False
        if self.players[self.current_player_index] != uid:
            return False

        if action == 'fold':
            self.player_data[uid]['folded'] = True
            self._move_to_next()
            return True
        elif action == 'check':
            if self.current_bet > self.round_bets[uid]:
                return False
            self._move_to_next()
            return True
        elif action == 'call':
            need = self.current_bet - self.round_bets[uid]
            if need <= 0:
                return False
            player = await db.get_player(uid)
            if not player or player['capital'] < need:
                return False
            await self._place_bet(uid, need)
            self._move_to_next()
            return True
        elif action == 'raise':
            total = self.current_bet + amount
            if total <= self.round_bets[uid]:
                return False
            player = await db.get_player(uid)
            if not player or player['capital'] < total - self.round_bets[uid]:
                return False
            await self._place_bet(uid, total - self.round_bets[uid])
            self.current_bet = total
            self.last_raiser = uid
            self._move_to_next()
            return True
        return False

    def _move_to_next(self):
        self.current_player_index = self._next_alive_player(self.current_player_index)
        if self.current_player_index == -1:
            self.stage = 'showdown'

    def is_round_finished(self) -> bool:
        alive = sum(1 for p in self.players if not self.player_data[p]['folded'])
        if alive <= 1:
            return True
        if self.last_raiser is not None:
            if all(self.round_bets[uid] == self.current_bet for uid in self.players if not self.player_data[uid]['folded']):
                return True
        return False

    def get_winner(self) -> Optional[int]:
        alive = [uid for uid in self.players if not self.player_data[uid]['folded']]
        if len(alive) == 1:
            return alive[0]
        if len(alive) == 0:
            return None
        best = alive[0]
        for uid in alive[1:]:
            cmp = compare_hands(self.player_data[uid]['hand'], self.community,
                                self.player_data[best]['hand'], self.community)
            if cmp > 0:
                best = uid
        return best

# ======================== ХРАНИЛИЩА ИГР ========================
poker_games: Dict[int, PokerGame] = {}
rps_games: Dict[int, Dict] = {}

# ======================== КЛАВИАТУРЫ ========================
def get_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 Управление Холдингом", callback_data="view_profile")],
        [InlineKeyboardButton(text="📊 Начать Поглощение (Ссылка)", callback_data="gen_link")]
    ])

def get_back_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

def get_poker_lobby_keyboard(chat_id: int):
    game = poker_games.get(chat_id)
    if not game:
        return None, None
    text = "🃏 **Покерный стол**\n\n"
    if game.stage == 'waiting':
        creator = db.get_player(game.creator_id)  # синхронно, заменим на await позже, но для простоты оставим
        text += f"Создатель: @{escape_md(creator['username']) if creator else 'Unknown'}\n"
        text += f"Игроки ({len(game.players)}/{MAX_PLAYERS}):\n"
        for uid in game.players:
            p = db.get_player(uid)
            if p:
                text += f"  - @{escape_md(p['username'])}\n"
        text += "\nНажмите 'Присоединиться' чтобы сесть за стол."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🪑 Присоединиться", callback_data=f"poker_join_{chat_id}")],
            [InlineKeyboardButton(text="🚀 Начать игру", callback_data=f"poker_start_{chat_id}")],
            [InlineKeyboardButton(text="🚪 Выйти", callback_data=f"poker_leave_{chat_id}")]
        ])
        return kb, text
    return None, None

# ======================== ОСНОВНЫЕ ОБРАБОТЧИКИ ========================
@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    user_id = message.from_user.id
    username = message.from_user.username
    args = command.args

    await db.create_player(user_id, username)

    if args and args.startswith("takeover_"):
        try:
            attacker_id = int(args.replace("takeover_", ""))
        except ValueError:
            attacker_id = None

        if attacker_id and attacker_id != user_id:
            victim = await db.get_player(user_id)
            attacker = await db.get_player(attacker_id)

            if victim and attacker:
                stolen = int(victim.get("capital", 0) * TAKEOVER_PERCENT)
                if stolen > 0:
                    await db.update_capital(user_id, -stolen)
                    await db.update_capital(attacker_id, stolen)

                    await message.answer(
                        f"📊 **ВРАЖДЕБНОЕ ПОГЛОЩЕНИЕ!**\n\n"
                        f"Инвестор @{escape_md(attacker.get('username', 'Unknown'))} перехватил ваши акции!\n"
                        f"💸 Потеря капитала: -{stolen} монет\n\n"
                        f"Жми кнопку ниже, чтобы сгенерировать ответную ссылку!",
                        parse_mode="Markdown", reply_markup=get_main_menu()
                    )
                    try:
                        await bot.send_message(
                            attacker_id,
                            f"🔥 **Сделка закрыта!**\nВы успешно поглотили активы @{escape_md(username or str(user_id))} и получили +{stolen} монет!"
                        )
                    except Exception:
                        pass
                else:
                    await message.answer("❌ У жертвы нет капитала для поглощения.")
            else:
                await message.answer("❌ Ошибка: данные о игроках не найдены.")
        else:
            if attacker_id == user_id:
                await message.answer("❌ Нельзя поглотить самого себя!")

    player = await db.get_player(user_id)
    if not player:
        await db.create_player(user_id, username)
        player = await db.get_player(user_id)

    if player:
        text = f"🏛 **MONOPOLY: SYNDICATE** 🏛\n\nДобро пожаловать на Уолл-стрит в Telegram.\nВаш оборотный капитал: **{player.get('capital', 0)} монет**"
        await message.answer(text, parse_mode="Markdown", reply_markup=get_main_menu())
    else:
        await message.answer("❌ Ошибка создания профиля. Попробуйте еще раз.")

@dp.callback_query(F.data == "view_profile")
async def view_profile(callback: CallbackQuery):
    user_id = callback.from_user.id
    player = await db.get_player(user_id)
    if not player:
        await callback.answer("❌ Профиль не найден. Нажмите /start", show_alert=True)
        return

    current_time = int(time.time())
    time_passed = current_time - player.get("last_collect", 0)

    if time_passed >= COOLDOWN_SECONDS:
        income_text = "💰 **Дивиденды готовы к сбору!**"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💵 Собрать прибыль (+{INCOME_AMOUNT} монет)", callback_data="collect_income")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
        ])
    else:
        time_left = COOLDOWN_SECONDS - time_passed
        income_text = f"⏳ Новые активы генерируются. Сбор доступен через **{time_left} сек.**"
        kb = get_back_button()

    safe_username = escape_md(player.get("username", "Unknown"))
    text = (f"🏢 **Ваш Финансовый Холдинг**\n\n"
            f"👤 Управляющий: @{safe_username}\n"
            f"💵 Текущий Капитал: **{player.get('capital', 0)} монет**\n"
            f"💼 Доля рынка: Активная инвестиция\n\n{income_text}")

    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logging.error(f"view_profile edit error: {e}")
    await callback.answer()

@dp.callback_query(F.data == "collect_income")
async def collect_income(callback: CallbackQuery):
    user_id = callback.from_user.id
    player = await db.get_player(user_id)
    if not player:
        await callback.answer("❌ Профиль не найден", show_alert=True)
        return

    current_time = int(time.time())
    if current_time - player.get("last_collect", 0) >= COOLDOWN_SECONDS:
        await db.update_capital(user_id, INCOME_AMOUNT)
        await db.update_collect_time(user_id, current_time)
        await callback.answer(f"💵 +{INCOME_AMOUNT} монет зачислено!", show_alert=True)
    else:
        await callback.answer("❌ Время еще не пришло!", show_alert=True)

    await view_profile(callback)

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    player = await db.get_player(user_id)
    if not player:
        await callback.answer("❌ Профиль не найден", show_alert=True)
        return

    text = f"🏛 **MONOPOLY: SYNDICATE** 🏛\n\nВаш оборотный капитал: **{player.get('capital', 0)} монет**"
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=get_main_menu())
    except Exception as e:
        logging.error(f"back_to_menu edit error: {e}")
    await callback.answer()

@dp.callback_query(F.data == "gen_link")
async def gen_link(callback: CallbackQuery):
    user_id = callback.from_user.id
    try:
        bot_username = (await bot.get_me()).username
    except Exception as e:
        logging.error(f"get_me error: {e}")
        await callback.answer("❌ Ошибка сервера", show_alert=True)
        return

    takeover_link = f"https://t.me/{bot_username}?start=takeover_{user_id}"
    text = (f"💼 **Протокол Захвата Активов**\n\n"
            f"Скопируйте инсайдерскую ссылку ниже и заманите друга.\n\n"
            f"🔗 Ссылка-западня:\n`{takeover_link}`")

    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=get_back_button())
    except Exception as e:
        logging.error(f"gen_link edit error: {e}")
    await callback.answer()

# ======================== БАЛАНС И ПЕРЕДАЧА МОНЕТ ========================
@dp.message(F.text.in_({'.б', '.баланс'}))
async def show_balance(message: Message):
    user_id = message.from_user.id
    player = await db.get_player(user_id)
    if not player:
        await db.create_player(user_id, message.from_user.username)
        player = await db.get_player(user_id)
    if player:
        await message.reply(f"💰 Ваш баланс: **{player.get('capital', 0)} монет**", parse_mode="Markdown")
    else:
        await message.reply("❌ Ошибка получения баланса")

@dp.message(F.text.startswith(('.дать', '.передать')))
async def transfer_money(message: Message):
    """
    Форматы:
    .дать 100 @username
    .передать 100 username
    .дать 100 (с ответом на сообщение получателя)
    .передать 100 (с ответом)
    """
    user_id = message.from_user.id
    sender = await db.get_player(user_id)
    if not sender:
        await message.reply("❌ У вас нет профиля. Напишите /start.")
        return

    text = message.text.strip()
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        await message.reply("❌ Использование: `.дать [сумма] [@username]` или ответьте на сообщение получателя.")
        return

    try:
        amount = int(parts[1])
    except ValueError:
        await message.reply("❌ Сумма должна быть целым числом.")
        return

    if amount <= 0:
        await message.reply("❌ Сумма должна быть положительной.")
        return

    if sender['capital'] < amount:
        await message.reply(f"❌ Недостаточно средств. У вас {sender['capital']} монет.")
        return

    recipient = None
    if message.reply_to_message:
        recipient_id = message.reply_to_message.from_user.id
        recipient = await db.get_player(recipient_id)
        if not recipient:
            await message.reply("❌ У получателя нет профиля. Попросите его написать /start.")
            return
    else:
        if len(parts) < 3:
            await message.reply("❌ Укажите получателя: `.дать 100 @username` или ответьте на его сообщение.")
            return
        username = parts[2].strip()
        if username.startswith('@'):
            username = username[1:]
        recipient = await db.get_user_by_username(username)
        if not recipient:
            await message.reply(f"❌ Пользователь @{username} не найден в базе. Возможно, он ещё не запускал бота.")
            return

    if recipient['user_id'] == user_id:
        await message.reply("❌ Нельзя передать монеты самому себе.")
        return

    await db.update_capital(user_id, -amount)
    await db.update_capital(recipient['user_id'], amount)

    await message.reply(
        f"✅ Вы успешно передали **{amount} монет** @{escape_md(recipient['username'])}.\n"
        f"Ваш новый баланс: **{sender['capital'] - amount} монет**.",
        parse_mode="Markdown"
    )

    try:
        await bot.send_message(
            recipient['user_id'],
            f"💰 @{escape_md(message.from_user.username)} перевел(а) вам **{amount} монет**.\n"
            f"Ваш новый баланс: **{recipient['capital'] + amount} монет**.",
            parse_mode="Markdown"
        )
    except Exception:
        pass

# ======================== ГРУППОВЫЕ КОМАНДЫ (ПОКЕР) ========================
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text.startswith('.покер'))
async def poker_command(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    await db.create_player(user_id, message.from_user.username)
    if chat_id in poker_games:
        game = poker_games[chat_id]
        if game.started and game.stage != 'waiting':
            await message.reply("🃏 Игра уже идёт! Дождитесь окончания.")
            return
        if game.add_player(user_id):
            await message.reply(f"✅ @{escape_md(message.from_user.username)} присоединился к столу!")
        else:
            await message.reply("❌ Не удалось присоединиться (возможно, вы уже в игре или стол полон).")
    else:
        game = PokerGame(chat_id, user_id)
        game.add_player(user_id)
        poker_games[chat_id] = game
        await message.reply("🃏 **Покерный стол создан!**\nНажмите 'Присоединиться' чтобы играть.", parse_mode="Markdown")
    await update_poker_lobby(chat_id)

@dp.callback_query(F.data.startswith("poker_join_"))
async def poker_join(callback: CallbackQuery):
    chat_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    await db.create_player(user_id, callback.from_user.username)
    game = poker_games.get(chat_id)
    if not game:
        await callback.answer("❌ Игра не найдена", show_alert=True)
        return
    if game.stage != 'waiting':
        await callback.answer("❌ Игра уже началась", show_alert=True)
        return
    if game.add_player(user_id):
        await callback.answer("✅ Вы присоединились к столу!")
    else:
        await callback.answer("❌ Не удалось присоединиться", show_alert=True)
    await update_poker_lobby(chat_id)

@dp.callback_query(F.data.startswith("poker_leave_"))
async def poker_leave(callback: CallbackQuery):
    chat_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    game = poker_games.get(chat_id)
    if not game:
        await callback.answer("❌ Игра не найдена", show_alert=True)
        return
    if game.stage != 'waiting':
        await callback.answer("❌ Игра уже началась, нельзя выйти", show_alert=True)
        return
    game.remove_player(user_id)
    await callback.answer("🚪 Вы покинули стол")
    if len(game.players) == 0:
        del poker_games[chat_id]
        await callback.message.edit_text("🃏 Стол закрыт (все игроки вышли).")
        return
    await update_poker_lobby(chat_id)

@dp.callback_query(F.data.startswith("poker_start_"))
async def poker_start(callback: CallbackQuery):
    chat_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    game = poker_games.get(chat_id)
    if not game:
        await callback.answer("❌ Игра не найдена", show_alert=True)
        return
    if game.creator_id != user_id:
        await callback.answer("❌ Только создатель может начать игру", show_alert=True)
        return
    if game.stage != 'waiting':
        await callback.answer("❌ Игра уже началась", show_alert=True)
        return
    if len(game.players) < MIN_PLAYERS:
        await callback.answer(f"❌ Нужно минимум {MIN_PLAYERS} игроков", show_alert=True)
        return
    for uid in game.players:
        p = await db.get_player(uid)
        if p['capital'] < ANTE:
            await callback.answer(f"❌ У @{escape_md(p['username'])} недостаточно средств для анте ({ANTE} монет)", show_alert=True)
            return
    if not game.start_game():
        await callback.answer("❌ Не удалось начать игру", show_alert=True)
        return
    await callback.answer("🚀 Игра началась!")
    await send_poker_phase(chat_id)

async def update_poker_lobby(chat_id: int):
    game = poker_games.get(chat_id)
    if not game:
        return
    kb, text = get_poker_lobby_keyboard(chat_id)
    if kb is None:
        return
    await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)

async def send_poker_phase(chat_id: int):
    game = poker_games.get(chat_id)
    if not game:
        return
    comm_cards = ' '.join(game.community) if game.community else '❌'
    text = f"🃏 **Покер** (стадия: {game.stage.upper()})\n"
    text += f"Общие карты: {comm_cards}\n"
    text += f"Банк: {game.pot} монет\n"
    text += f"Текущая ставка: {game.current_bet} монет\n"
    for uid in game.players:
        p = await db.get_player(uid)
        status = "✅" if not game.player_data[uid]['folded'] else "❌ сбросил"
        text += f"@{escape_md(p['username'])} {status}\n"
    await bot.send_message(chat_id, text, parse_mode="Markdown")
    for uid in game.players:
        if game.player_data[uid]['folded']:
            continue
        p = await db.get_player(uid)
        hand = ' '.join(game.player_data[uid]['hand'])
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Чек/Колл", callback_data=f"poker_action_{chat_id}_{uid}_call")],
            [InlineKeyboardButton(text="📈 Рейз (+100)", callback_data=f"poker_action_{chat_id}_{uid}_raise_100")],
            [InlineKeyboardButton(text="🏳️ Фолд", callback_data=f"poker_action_{chat_id}_{uid}_fold")]
        ])
        msg = f"🃏 **Ваши карты**: {hand}\n"
        msg += f"Общие: {comm_cards}\n"
        msg += f"Банк: {game.pot} монет, ставка: {game.current_bet} монет\n"
        msg += "Ваш ход! Выберите действие:"
        await bot.send_message(uid, msg, parse_mode="Markdown", reply_markup=kb)
    current_uid = game.players[game.current_player_index] if game.current_player_index != -1 else None
    if current_uid:
        p = await db.get_player(current_uid)
        await bot.send_message(chat_id, f"⏳ Ход: @{escape_md(p['username'])}")

@dp.callback_query(F.data.startswith("poker_action_"))
async def poker_action(callback: CallbackQuery):
    data = callback.data.split("_")
    chat_id = int(data[2])
    uid = int(data[3])
    action = data[4]
    amount = int(data[5]) if len(data) > 5 else 0
    user_id = callback.from_user.id
    if user_id != uid:
        await callback.answer("❌ Это не ваш ход!", show_alert=True)
        return
    game = poker_games.get(chat_id)
    if not game or not game.started:
        await callback.answer("❌ Игра не активна", show_alert=True)
        return
    if game.stage == 'showdown':
        await callback.answer("❌ Игра уже завершена", show_alert=True)
        return
    if game.players[game.current_player_index] != uid:
        await callback.answer("❌ Сейчас не ваш ход", show_alert=True)
        return
    success = False
    if action == 'fold':
        success = await game.player_action(uid, 'fold')
    elif action == 'call':
        success = await game.player_action(uid, 'call')
    elif action == 'raise':
        success = await game.player_action(uid, 'raise', amount)
    else:
        await callback.answer("❌ Неизвестное действие")
        return
    if not success:
        await callback.answer("❌ Действие не выполнено (недостаточно средств или ошибка)", show_alert=True)
        return
    await callback.answer("✅ Действие принято")
    if game.is_round_finished():
        if game.stage != 'showdown':
            game._advance_stage()
            if game.stage == 'showdown':
                winner = game.get_winner()
                if winner is not None:
                    win_amount = game.pot
                    await db.update_capital(winner, win_amount)
                    p = await db.get_player(winner)
                    await bot.send_message(chat_id, f"🏆 **Победитель**: @{escape_md(p['username'])}! Выигрыш: {win_amount} монет")
                else:
                    await bot.send_message(chat_id, "🤷‍♂️ Ничья? Банк возвращается? (это упрощённая версия)")
                del poker_games[chat_id]
                await bot.send_message(chat_id, "🃏 Игра окончена. Для новой игры напишите .покер")
                return
        else:
            winner = game.get_winner()
            if winner is not None:
                win_amount = game.pot
                await db.update_capital(winner, win_amount)
                p = await db.get_player(winner)
                await bot.send_message(chat_id, f"🏆 **Победитель**: @{escape_md(p['username'])}! Выигрыш: {win_amount} монет")
            else:
                await bot.send_message(chat_id, "🤷‍♂️ Ничья")
            del poker_games[chat_id]
            await bot.send_message(chat_id, "🃏 Игра окончена. Для новой игры напишите .покер")
            return
    await send_poker_phase(chat_id)

# ======================== КАМЕНЬ-НОЖНИЦЫ-БУМАГА ========================
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text.startswith('.кнб'))
async def rps_command(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("❌ Использование: `.кнб [ставка]`", parse_mode="Markdown")
        return
    try:
        bet = int(parts[1])
    except ValueError:
        await message.reply("❌ Ставка должна быть целым числом.")
        return
    if bet <= 0:
        await message.reply("❌ Ставка должна быть положительной.")
        return

    user_id = message.from_user.id
    player = await db.get_player(user_id)
    if not player:
        await message.reply("❌ Сначала запустите бота в личке командой /start.")
        return
    if player['capital'] < bet:
        await message.reply(f"❌ Недостаточно средств. У вас {player['capital']} монет, а ставка {bet} монет.")
        return

    if user_id in rps_games:
        await message.reply("⚠️ У вас уже есть активная игра. Завершите её.")
        return

    rps_games[user_id] = {'bet': bet, 'status': 'waiting'}

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪨 Камень", callback_data=f"rps_{user_id}_rock")],
        [InlineKeyboardButton(text="✂️ Ножницы", callback_data=f"rps_{user_id}_scissors")],
        [InlineKeyboardButton(text="📄 Бумага", callback_data=f"rps_{user_id}_paper")]
    ])
    await message.reply(
        f"🎮 **Камень-ножницы-бумага**\nСтавка: {bet} монет\nВыбери свой ход:",
        parse_mode="Markdown",
        reply_markup=kb
    )

@dp.callback_query(F.data.startswith("rps_"))
async def rps_callback(callback: CallbackQuery):
    data = callback.data.split("_")
    user_id = int(data[1])
    choice = data[2]  # rock, scissors, paper

    if callback.from_user.id != user_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return

    game = rps_games.get(user_id)
    if not game or game['status'] != 'waiting':
        await callback.answer("❌ Игра не найдена или уже завершена.", show_alert=True)
        return

    del rps_games[user_id]

    bot_choice = random.choice(['rock', 'scissors', 'paper'])

    result = None
    if choice == bot_choice:
        result = 'draw'
    elif (choice == 'rock' and bot_choice == 'scissors') or \
         (choice == 'scissors' and bot_choice == 'paper') or \
         (choice == 'paper' and bot_choice == 'rock'):
        result = 'win'
    else:
        result = 'lose'

    bet = game['bet']
    if result == 'win':
        await db.update_capital(user_id, bet)
    elif result == 'lose':
        await db.update_capital(user_id, -bet)

    emoji = {'rock': '🪨', 'scissors': '✂️', 'paper': '📄'}
    bot_emoji = emoji[bot_choice]
    player_emoji = emoji[choice]

    if result == 'win':
        text = f"🎉 **Вы победили!**\nВаш ход: {player_emoji}\nБот: {bot_emoji}\nВы выиграли **{bet} монет**!"
    elif result == 'lose':
        text = f"😔 **Вы проиграли.**\nВаш ход: {player_emoji}\nБот: {bot_emoji}\nВы потеряли **{bet} монет**."
    else:
        text = f"🤝 **Ничья!**\nВаш ход: {player_emoji}\nБот: {bot_emoji}\nСтавка возвращена."

    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer()

# ======================== ЗАПУСК ========================
async def main():
    await db.connect()
    await db.init_tables()
    logging.info("🚀 Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())