import discord
from discord.ext import commands, tasks
from discord.ui import Button, View
import yfinance as yf
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import sqlite3
from io import BytesIO
from currency_converter import CurrencyConverter
from forex_python.converter import CurrencyRates
import math
from datetime import datetime, time, timedelta, date
import pytz
import asyncio
from pandas.tseries.holiday import USFederalHolidayCalendar
import numpy as np
import mplfinance as mpf


# 디스코드 봇 설정
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='w!', intents=intents)

# 데이터베이스 연결
conn = sqlite3.connect('database.db')
c = conn.cursor()

# users 테이블이 없으면 생성
c.execute('''
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    balance REAL,
    initial_balance REAL,
    total_bonus REAL DEFAULT 0,
    last_bonus_time TEXT
)
''')
conn.commit()

# stocks 테이블이 없으면 생성
c.execute('''
CREATE TABLE IF NOT EXISTS stocks (
    user_id INTEGER,
    stock_symbol TEXT,
    shares INTEGER,
    average_price REAL DEFAULT 0,
    PRIMARY KEY (user_id, stock_symbol),
    FOREIGN KEY(user_id) REFERENCES users(id)
)
''')
conn.commit()

# stock_data 테이블이 없으면 생성
c.execute('''
CREATE TABLE IF NOT EXISTS stock_data (
    symbol TEXT,
    date TEXT,
    close REAL,
    PRIMARY KEY (symbol, date)
)
''')
conn.commit()

# limit_orders 테이블이 없으면 생성
c.execute('''
CREATE TABLE IF NOT EXISTS limit_orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    symbol TEXT,
    shares INTEGER,
    price REAL,
    order_type TEXT,
    timestamp TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
)
''')
conn.commit()

# transactions 테이블을 생성
c.execute('''
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    stock_symbol TEXT,
    shares INTEGER,
    price REAL,
    type TEXT, -- 'buy' or 'sell'
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id)
)
''')

conn.commit()

# 거래 내역을 저장하기
def record_transaction(user_id, stock_symbol, shares, price, transaction_type):
    c = conn.cursor()
    
    c.execute('''
    INSERT INTO transactions (user_id, stock_symbol, shares, price, type)
    VALUES (?, ?, ?, ?, ?)
    ''', (user_id, stock_symbol, shares, price, transaction_type))
    
    conn.commit()

# 주식 가격 조회
def get_stock_price(symbol):
    stock = yf.Ticker(symbol)
    todays_data = stock.history(period='1d')
    if not todays_data.empty:
        return todays_data['Close'].iloc[0]
    else:
        raise ValueError("주식 가격 데이터를 가져올 수 없습니다.")
    
# 금액 포맷 함수
def format_currency(value):
    return f"{value:,.2f}"

# 환율 변환 함수
def convert_currency(amount, from_currency, to_currency):
    c = CurrencyConverter()
    return c.convert(amount, from_currency, to_currency)

# 보너스 쿨다운 체크 함수
def check_bonus_cooldown(last_bonus_time):
    if last_bonus_time:
        cooldown_end = datetime.fromisoformat(last_bonus_time) + timedelta(days=1)
        return datetime.now(tz=pytz.UTC) >= cooldown_end
    return True

def is_holiday(date):
    cal = USFederalHolidayCalendar()
    holidays = cal.holidays(start=date.replace(year=date.year-1), end=date.replace(year=date.year+1))
    return date in holidays

# 시장 오픈 여부 체크 함수
def is_market_open():
    # 현재 시간 얻기
    now = datetime.now(pytz.timezone('US/Eastern'))

    # 요일 확인 (0 = 월요일, ..., 6 = 일요일)
    if now.weekday() == 5 and now.time() >= time(5, 0):
        return False
    if now.weekday() == 6:
        return False

    # 거래 가능 시간 정의 (EST/EDT)
    premarket_open = time(4, 0)
    aftermarket_close = time(20, 0)

    # 현재 시간이 거래 가능 시간인지 확인
    if not (premarket_open <= now.time() <= aftermarket_close):
        return False

    # 휴장일 확인
    if is_holiday(now.date()):
        return False

    return True

# 유저 등록
@bot.command(name='등록')
async def register(ctx):
    user_id = ctx.author.id
    c.execute("SELECT 1 FROM users WHERE id=?", (user_id,))
    if c.fetchone():
        await ctx.reply(embed=discord.Embed(description=f"{ctx.author.display_name}님은 이미 등록되었습니다.", color=discord.Color.red()))
        return
    initial_balance_usd = 1000
    balance_krw = convert_currency(initial_balance_usd, 'USD', 'KRW')
    c.execute("INSERT INTO users (id, balance, initial_balance) VALUES (?, ?, ?)", (user_id, initial_balance_usd, initial_balance_usd))
    conn.commit()
    user = await bot.fetch_user(user_id)
    await ctx.reply(embed=discord.Embed(description=f"{user.display_name} 등록 완료! 초기 잔액은 ${initial_balance_usd} (원화: {format_currency(balance_krw)}원)입니다.", color=discord.Color.green()))


# 자산 명령어
@bot.command(name='자산')
async def assets(ctx):
    user_id = ctx.author.id
    c.execute("SELECT balance, initial_balance, total_bonus FROM users WHERE id=?", (user_id,))
    result = c.fetchone()
    if not result:
        await ctx.reply(embed=discord.Embed(description="등록되지 않은 사용자입니다. 먼저 `w!등록` 명령어로 등록해주세요.", color=discord.Color.red()))
        return

    balance, initial_balance, total_bonus = result
    c.execute("SELECT stock_symbol, shares, average_price FROM stocks WHERE user_id=?", (user_id,))
    stocks = c.fetchall()

    # Calculate total value in KRW and USD
    total_balance_usd = balance
    stock_details = []

    for stock_symbol, shares, average_price in stocks:
        stock = yf.Ticker(stock_symbol)
        stock_info = stock.history(period="1d")
        if not stock_info.empty:
            current_price = stock_info['Close'].iloc[-1]
            total_stock_value = shares * current_price
            total_balance_usd += total_stock_value
            price_krw = convert_currency(current_price, 'USD', 'KRW')
            stock_value_krw = convert_currency(total_stock_value, 'USD', 'KRW')
            profit_rate = ((current_price - average_price) / average_price) * 100
            stock_details.append(f"{stock_symbol}: {shares}주 (현재 가격: {format_currency(price_krw)}원 (${current_price:.2f}), 가치: {format_currency(stock_value_krw)}원 (${total_stock_value:.2f}), 수익률: {profit_rate:.2f}%)")

    total_balance_krw = convert_currency(total_balance_usd, 'USD', 'KRW')
    initial_balance_krw = convert_currency(initial_balance, 'USD', 'KRW')

    # Calculate profit rate excluding bonus and pending limit orders
    c.execute("SELECT SUM(shares * price) FROM limit_orders WHERE user_id=? AND order_type='buy'", (user_id,))
    pending_buy_orders = c.fetchone()[0] or 0

    # Sum of investments excluding pending buy orders
    investments_sum = initial_balance + sum(average_price * shares for _, shares, average_price in stocks)
    
    # Calculate net profit excluding bonuses and pending buy orders
    net_profit_usd = total_balance_usd - investments_sum - total_bonus
    
    # Calculate profit rate
    profit_rate = (net_profit_usd / investments_sum) * 100 if investments_sum != 0 else 0

    embed = discord.Embed(title=f"{ctx.author.display_name}님의 자산 현황", color=discord.Color.blue())
    embed.add_field(name="잔고", value=f"${format_currency(balance)} (₩{format_currency(convert_currency(balance, 'USD', 'KRW'))})", inline=False)

    if stock_details:
        for detail in stock_details:
            embed.add_field(name="보유 주식", value=detail, inline=False)

    embed.add_field(name="총 자산", value=f"${format_currency(total_balance_usd)} (₩{format_currency(total_balance_krw)}) ({profit_rate:.2f}%)", inline=False)

    await ctx.reply(embed=embed)

    # 자산 페이지네이션 함수
async def paginate_assets(ctx, embed, stock_details, page=0):
    max_per_page = 10
    stock_page = stock_details[page * max_per_page:(page + 1) * max_per_page]

    for detail in stock_page:
        embed.add_field(name="\u200b", value=detail, inline=False)

    view = View()
    if page > 0:
        prev_button = Button(label="이전", style=discord.ButtonStyle.primary)
        prev_button.callback = lambda interaction: paginate_assets(ctx, embed, stock_details, page - 1)
        view.add_item(prev_button)

    if (page + 1) * max_per_page < len(stock_details):
        next_button = Button(label="다음", style=discord.ButtonStyle.primary)
        next_button.callback = lambda interaction: paginate_assets(ctx, embed, stock_details, page + 1)
        view.add_item(next_button)

    await ctx.reply(embed=embed, view=view)

class StockView(View):
    def __init__(self, symbol):
        super().__init__(timeout=300)  # 5분 후 만료
        self.symbol = symbol.upper()

    async def update_graph(self, ctx):
        # 주식 데이터 가져오기
        stock = yf.Ticker(self.symbol)
        hist = stock.history(period="3mo", interval="5d")

        if hist.empty:
            await ctx.reply("주식 데이터를 가져올 수 없습니다.")
            return

        # 캔들 차트 생성
        fig, ax = plt.subplots()
        hist['Date'] = mdates.date2num(hist.index.to_pydatetime())
        ohlc = hist[['Date', 'Open', 'High', 'Low', 'Close']]
        mpf.plot(ohlc, type='candle', ax=ax, style='charles')
        ax.set_title(f"{self.symbol} 3 Months candle")
        ax.set_xlabel('Date')
        ax.set_ylabel('Price (USD)')

        # 그래프를 임시 파일에 저장
        buf = BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        plt.close(fig)

        # 이미지를 디스코드에 업로드하고 메시지 업데이트
        file = discord.File(buf, filename=f"{self.symbol}_chart.png")
        price = hist['Close'].iloc[-1]
        price_krw = convert_currency(price, 'USD', 'KRW')
        embed = discord.Embed(
            title=f"{self.symbol} 주식 정보",
            description=f"현재 가격: ${price:.2f} ({format_currency(price_krw)}원)",
            color=discord.Color.blue()
        )
        embed.set_image(url=f"attachment://{self.symbol}_chart.png")

        await ctx.reply(embed=embed, file=file, view=self)

@bot.command(name='주식')
async def stock(ctx, symbol: str):
    symbol = symbol.upper()  # 심볼을 대문자로 변환
    view = StockView(symbol)
    await view.update_graph(ctx)  # 초기 그래프 생성 및 메시지 전송


#즉시구매하기
@bot.command(name='구매')
async def buy(ctx, symbol: str, shares: int):
    if not is_market_open():  
        await ctx.reply(embed=discord.Embed(description="시장 시간이 아닙니다. 시장이 열렸을 때 시도해주세요.(프리마켓 ~ 애프터마켓의 영업일인 평일)", color=discord.Color.red()))
        return

    symbol = symbol.upper()
    user_id = ctx.author.id
    price = get_stock_price(symbol)
    if price:
        total_cost_usd = price * shares
        c.execute("SELECT balance FROM users WHERE id=?", (user_id,))
        result = c.fetchone()
        if result:
            balance = result[0]
            if balance >= total_cost_usd:
                average_price = total_cost_usd / shares

                c.execute("UPDATE users SET balance = balance - ? WHERE id=?", (total_cost_usd, user_id))
                c.execute("""
                INSERT INTO stocks (user_id, stock_symbol, shares, average_price)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, stock_symbol)
                DO UPDATE SET shares = shares + ?, average_price = (average_price * shares + ?)/(shares + ?)
                """, (user_id, symbol, shares, average_price, shares, total_cost_usd, shares))

                conn.commit()
                await ctx.reply(embed=discord.Embed(
                    description=f"""
                    **{ctx.author.display_name}님이 {symbol} 주식을 {shares}주 구매했습니다.**
                    **총 비용: ${total_cost_usd:,.2f}**
                    **1주당 평균 가격: ${price:,.2f}**
                    """,
                    color=discord.Color.green()
                ))
            else:
                await ctx.reply(embed=discord.Embed(
                    description=f"""
                    **{ctx.author.display_name}님의 잔액이 부족합니다.**
                    **현재 소지금: ${balance:,.2f}**
                    **주문한 총 주식 금액: ${total_cost_usd:,.2f}**
                    """,
                    color=discord.Color.red()
                ))
        else:
            await ctx.reply(embed=discord.Embed(description=f"**{ctx.author.display_name} 등록되지 않았습니다. w!등록 명령어로 등록해주세요.**", color=discord.Color.red()))
    else:
        await ctx.reply(embed=discord.Embed(description=f"**유효하지 않은 주식 기호입니다.**", color=discord.Color.red()))

@bot.command(name='판매')
async def buy(ctx, symbol: str, shares: int):
    if not is_market_open():  
        await ctx.reply(embed=discord.Embed(description="시장 시간이 아닙니다. 시장이 열렸을 때 시도해주세요.(프리마켓 ~ 애프터마켓의 영업일인 평일)", color=discord.Color.red()))
        return

    symbol = symbol.upper()
    user_id = ctx.author.id
    price = get_stock_price(symbol)
    if price:
        c.execute("SELECT shares, average_price FROM stocks WHERE user_id=? AND stock_symbol=?", (user_id, symbol))
        result = c.fetchone()
        if result:
            current_shares, average_price = result
            if shares <= current_shares:
                total_sale_usd = price * shares
                total_sale_usd_after_fee = total_sale_usd * 0.999  # 수수료 0.1% 적용
                c.execute("UPDATE users SET balance = balance + ? WHERE id=?", (total_sale_usd_after_fee, user_id))
                if shares == current_shares:
                    c.execute("DELETE FROM stocks WHERE user_id=? AND stock_symbol=?", (user_id, symbol))
                else:
                    c.execute("UPDATE stocks SET shares = shares - ? WHERE user_id=? AND stock_symbol=?", (shares, user_id, symbol))

                conn.commit()
                original_investment_usd = average_price * shares
                total_profit_usd = total_sale_usd_after_fee - original_investment_usd
                profit_rate = (total_profit_usd / original_investment_usd) * 100

                record_transaction(user_id, symbol, shares, price, 'sell')

                await ctx.reply(embed=discord.Embed(
                    description=f"""
                    **{ctx.author.display_name}님이 {symbol} 주식을 {shares}주 판매했습니다.**
                    **1주당 판매 가격: ${price:,.2f}**
                    **총 판매 금액: ${total_sale_usd:,.2f}**
                    **수수료 후 금액: ${total_sale_usd_after_fee:,.2f}**
                    **최종 수익: ${total_profit_usd:,.2f}**
                    **수익률: {profit_rate:.2f}%**
                    """,
                    color=discord.Color.green()
                ))
            else:
                await ctx.reply(embed=discord.Embed(description=f"**{ctx.author.display_name}님의 보유 주식 수량이 부족합니다.**", color=discord.Color.red()))
        else:
            await ctx.reply(embed=discord.Embed(description=f"**{symbol} 주식을 보유하고 있지 않습니다.**", color=discord.Color.red()))
    else:
        await ctx.reply(embed=discord.Embed(description=f"**유효하지 않은 주식 기호입니다.**", color=discord.Color.red()))

'''
@bot.command(name='예약매수')
async def limit_buy(ctx, symbol: str, shares: int, price: float):
    if not is_market_open():  
        await ctx.reply(embed=discord.Embed(description="시장 시간이 아닙니다. 시장이 열렸을 때 시도해주세요.(프리마켓 ~ 애프터마켓의 영업일인 평일)", color=discord.Color.red()))
        return
    
    if price <= 0 or len(str(price).split('.')[-1]) > 2:
        await ctx.reply(embed=discord.Embed(description="가격은 0보다 커야 하며 소수점 두 자리까지 입력해야 합니다.", color=discord.Color.red()))
        return
    
    user_id = ctx.author.id
    c.execute("SELECT balance FROM users WHERE id=?", (user_id,))
    result = c.fetchone()
    if result:
        balance = result[0]
    else:
        await ctx.reply(embed=discord.Embed(description="사용자 정보를 찾을 수 없습니다.", color=discord.Color.red()))
        return
    
    total_cost = shares * price
    if shares <= 0 or total_cost > balance:
        await ctx.reply(embed=discord.Embed(description="구매하려는 주식 수량이 잔고를 초과하거나 0보다 작습니다.", color=discord.Color.red()))
        return
    
    now = datetime.now(tz=pytz.UTC).isoformat()
    c.execute("INSERT INTO limit_orders (user_id, symbol, shares, price, order_type, timestamp) VALUES (?, ?, ?, ?, 'buy', ?)", 
              (user_id, symbol.upper(), shares, price, now))
    c.execute("UPDATE users SET balance = balance - ? WHERE id=?", (total_cost, user_id))
    conn.commit()
    await ctx.reply(embed=discord.Embed(
        description=f"{symbol.upper()} 주식을 {shares}주, 주당 ${price:.2f}에 예약 매수했습니다. 이 예약은 24시간 동안 유효합니다.",
        color=discord.Color.green()
    ))



# 지정가 매도
@bot.command(name='예약매도')
async def limit_sell(ctx, symbol: str, shares: int, price: float):
    if not is_market_open():  
        await ctx.reply(embed=discord.Embed(description="시장 시간이 아닙니다. 시장이 열렸을 때 시도해주세요.(프리마켓 ~ 애프터마켓의 영업일인 평일)", color=discord.Color.red()))
        return
    
    if price <= 0 or len(str(price).split('.')[-1]) > 2:
        await ctx.reply(embed=discord.Embed(description="가격은 0보다 커야 하며 소수점 두 자리까지 입력해야 합니다.", color=discord.Color.red()))
        return
    
    user_id = ctx.author.id
    c.execute("SELECT COALESCE(SUM(shares), 0) FROM stocks WHERE user_id=? AND stock_symbol=?", (user_id, symbol.upper()))
    result = c.fetchone()
    current_shares = result[0] if result else 0
    
    if shares <= 0 or shares > current_shares:
        await ctx.reply(embed=discord.Embed(description="판매하려는 주식 수량이 보유한 수량을 초과하거나 0보다 작습니다.", color=discord.Color.red()))
        return
    
    now = datetime.now(tz=pytz.UTC).isoformat()
    c.execute("INSERT INTO limit_orders (user_id, symbol, shares, price, order_type, timestamp) VALUES (?, ?, ?, ?, 'sell', ?)", 
              (user_id, symbol.upper(), shares, price, now))
    conn.commit()
    await ctx.reply(embed=discord.Embed(
        description=f"{symbol.upper()} 주식을 {shares}주, 주당 ${price:.2f}에 예약 매도했습니다. 이 예약은 24시간 동안 유효합니다.",
        color=discord.Color.green()
    ))

# 예약 확인
@bot.command(name='예약확인')
async def check_orders(ctx):
    user_id = ctx.author.id
    now = datetime.now(tz=pytz.UTC)
    c.execute("SELECT order_id, symbol, shares, price, order_type, timestamp FROM limit_orders WHERE user_id=?", (user_id,))
    orders = c.fetchall()
    if orders:
        embed = discord.Embed(title=f"{ctx.author.display_name}님의 예약 주문", color=discord.Color.blue())
        for order_id, symbol, shares, price, order_type, timestamp in orders:
            order_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            if (now - order_time) > timedelta(hours=24):
                c.execute("DELETE FROM limit_orders WHERE order_id=?", (order_id,))
                continue
            time_left = timedelta(hours=24) - (now - order_time)
            embed.add_field(name=f"주문 ID: {order_id}", value=f"종류: {order_type}, 종목: {symbol}, 수량: {shares}, 가격: ${price:.2f}, 남은 시간: {time_left}", inline=False)
        conn.commit()
        await ctx.reply(embed=embed)
    else:
        await ctx.reply(embed=discord.Embed(description=f"예약된 주문이 없습니다.", color=discord.Color.red()))

# 예약 취소
@bot.command(name='예약취소')
async def cancel_order(ctx, order_id: int):
    user_id = ctx.author.id
    c.execute("SELECT user_id, symbol, shares, price, order_type FROM limit_orders WHERE order_id=? AND user_id=?", (order_id, user_id))
    result = c.fetchone()
    if result:
        _, symbol, shares, price, order_type = result
        if order_type == 'buy':
            total_cost = shares * price
            c.execute("UPDATE users SET balance = balance + ? WHERE id=?", (total_cost, user_id))
        c.execute("DELETE FROM limit_orders WHERE order_id=?", (order_id,))
        conn.commit()
        await ctx.reply(embed=discord.Embed(description=f"주문 ID {order_id}(이)가 성공적으로 취소되었습니다.", color=discord.Color.green()))
    else:
        await ctx.reply(embed=discord.Embed(description=f"유효하지 않은 주문 ID입니다.", color=discord.Color.red()))

# 예약 주문 처리 태스크
@tasks.loop(minutes=1)
async def process_limit_orders():
    now = datetime.now(tz=pytz.UTC)
    c.execute("SELECT order_id, user_id, symbol, shares, price, order_type, timestamp FROM limit_orders")
    orders = c.fetchall()
    for order in orders:
        order_id, user_id, symbol, shares, price, order_type, timestamp = order
        order_time = datetime.fromisoformat(timestamp)
        if (now - order_time) > timedelta(hours=24):
            if order_type == 'buy':
                total_cost = shares * price
                c.execute("UPDATE users SET balance = balance + ? WHERE id=?", (total_cost, user_id))
            c.execute("DELETE FROM limit_orders WHERE order_id=?", (order_id,))
            await bot.fetch_user(user_id).send(f"주문 ID {order_id}가 만료되었습니다.")
            continue

        current_price = get_stock_price(symbol)
        if (order_type == 'buy' and current_price <= price) or (order_type == 'sell' and current_price >= price):
            if order_type == 'buy':
                c.execute("SELECT balance FROM users WHERE id=?", (user_id,))
                balance = c.fetchone()[0]
                total_cost = shares * current_price
                if balance >= total_cost:
                    c.execute("UPDATE users SET balance = balance - ? WHERE id=?", (total_cost, user_id))
                    c.execute("SELECT shares, average_price FROM stocks WHERE user_id=? AND stock_symbol=?", (user_id, symbol))
                    result = c.fetchone()
                    if result:
                        current_shares, current_avg_price = result
                        new_shares = current_shares + shares
                        new_avg_price = ((current_shares * current_avg_price) + (shares * current_price)) / new_shares
                        c.execute("UPDATE stocks SET shares = ?, average_price = ? WHERE user_id=? AND stock_symbol=?", 
                                  (new_shares, new_avg_price, user_id, symbol))
                    else:
                        c.execute("INSERT INTO stocks (user_id, stock_symbol, shares, average_price) VALUES (?, ?, ?, ?)", 
                                  (user_id, symbol, shares, current_price))
                    record_transaction(user_id, symbol, shares, current_price, 'buy')
                    await bot.fetch_user(user_id).send(f"주문 ID {order_id}가 성사되었습니다. {shares}주를 주당 ${current_price:.2f}에 구매했습니다.")
            
            elif order_type == 'sell':
                c.execute("SELECT shares, average_price FROM stocks WHERE user_id=? AND stock_symbol=?", (user_id, symbol))
                result = c.fetchone()
                if result:
                    current_shares, current_avg_price = result
                    if shares <= current_shares:
                        total_revenue = shares * current_price
                        c.execute("UPDATE users SET balance = balance + ? WHERE id=?", (total_revenue, user_id))
                        new_shares = current_shares - shares
                        if new_shares > 0:
                            c.execute("UPDATE stocks SET shares = ? WHERE user_id=? AND stock_symbol=?", 
                                      (new_shares, user_id, symbol))
                        else:
                            c.execute("DELETE FROM stocks WHERE user_id=? AND stock_symbol=?", (user_id, symbol))
                        record_transaction(user_id, symbol, shares, current_price, 'sell')
                        await bot.fetch_user(user_id).send(f"주문 ID {order_id}가 성사되었습니다. {shares}주를 주당 ${current_price:.2f}에 판매했습니다.")
            c.execute("DELETE FROM limit_orders WHERE order_id=?", (order_id,))
    conn.commit()'''


# 보너스 명령어
@bot.command(name='보너스')
async def bonus(ctx):
    user_id = ctx.author.id
    c.execute("SELECT balance, total_bonus, last_bonus_time FROM users WHERE id=?", (user_id,))
    result = c.fetchone()
    if result:
        balance, total_bonus, last_bonus_time = result

        if not check_bonus_cooldown(last_bonus_time):
            cooldown_end = datetime.fromisoformat(last_bonus_time) + timedelta(days=1)
            remaining_time = cooldown_end - datetime.now(tz=pytz.UTC)
            await ctx.reply(embed=discord.Embed(
                description=f"{ctx.author.display_name}님, 아직 보너스를 받을 수 없습니다. 남은 시간: {remaining_time}.",
                color=discord.Color.red()
            ))
            return

        bonus_usd = 100
        total_bonus_usd = total_bonus + bonus_usd
        new_balance_usd = balance + bonus_usd
        now = datetime.now(tz=pytz.UTC).isoformat()

        c.execute("UPDATE users SET balance = ?, total_bonus = ?, last_bonus_time = ? WHERE id = ?", 
                  (new_balance_usd, total_bonus_usd, now, user_id))
        conn.commit()

        await ctx.reply(embed=discord.Embed(
            description=f"{ctx.author.display_name}님, 24시간 쿨타임이 지난 후 ${bonus_usd:,.2f}이 지급되었습니다.",
            color=discord.Color.green()
        ))
    else:
        await ctx.reply(embed=discord.Embed(description=f"{ctx.author.display_name} 등록되지 않았습니다. w!등록 명령어로 등록해주세요.", color=discord.Color.red()))



@bot.command(name='리더보드')
async def leaderboard(ctx):
    c.execute("SELECT id, balance, initial_balance, total_bonus FROM users")
    users = c.fetchall()

    leaderboard = []

    for user in users:
        user_id, balance, initial_balance, total_bonus = user
        c.execute("SELECT stock_symbol, shares, average_price FROM stocks WHERE user_id=?", (user_id,))
        stocks = c.fetchall()

        total_stock_value_usd = 0
        for symbol, shares, average_price in stocks:
            current_price = get_stock_price(symbol)
            if current_price:
                total_stock_value_usd += current_price * shares

        total_assets_usd = balance + total_stock_value_usd
        total_assets_krw = convert_currency(total_assets_usd, 'USD', 'KRW')

        net_investment_usd = initial_balance + sum(average_price * shares for _, shares, average_price in stocks)
        net_profit_usd = total_assets_usd - net_investment_usd - total_bonus
        profit_rate = (net_profit_usd / net_investment_usd) * 100 if net_investment_usd != 0 else 0

        leaderboard.append((user_id, total_assets_usd, total_assets_krw, profit_rate))

    leaderboard.sort(key=lambda x: x[3], reverse=True)

    await paginate_leaderboard(ctx, leaderboard)

async def paginate_leaderboard(ctx, leaderboard, page=0):
    max_per_page = 10
    total_pages = math.ceil(len(leaderboard) / max_per_page)

    embed = discord.Embed(title="리더보드", description=f"페이지 {page + 1}/{total_pages}", color=discord.Color.gold())
    start = page * max_per_page
    end = start + max_per_page
    for i, (user_id, total_assets_usd, total_assets_krw, profit_rate) in enumerate(leaderboard[start:end], start=start + 1):
        user = await bot.fetch_user(user_id)
        embed.add_field(name=f"{i}. {user.display_name}", value=f"총 자산: ${format_currency(total_assets_usd)} (₩{format_currency(total_assets_krw)})\n수익률: {profit_rate:.2f}%", inline=False)

    view = View()
    if page > 0:
        prev_button = Button(label="이전", style=discord.ButtonStyle.primary)
        async def prev_callback(interaction):
            await paginate_leaderboard(ctx, leaderboard, page - 1)
        prev_button.callback = prev_callback
        view.add_item(prev_button)

    if end < len(leaderboard):
        next_button = Button(label="다음", style=discord.ButtonStyle.primary)
        async def next_callback(interaction):
            await paginate_leaderboard(ctx, leaderboard, page + 1)
        next_button.callback = next_callback
        view.add_item(next_button)

    await ctx.reply(embed=embed, view=view)

@bot.command(name='도움말')
async def help(ctx):
    help_text = """
사용 가능한 명령어 목록:
w!등록 - 유저를 등록하고 초기 잔액을 설정합니다.
w!자산 - 현재 잔액과 보유 주식을 확인합니다.
w!주식 [심볼] - 주식의 현재 가격과 그래프를 보여줍니다.
w!구매 [심볼] [주식수] - 주식을 구매합니다. (시장가)
w!판매 [심볼] [주식수] - 주식을 판매합니다. (시장가)
w!보너스 - 24시간마다 보너스를 받습니다.
- 현재 개발중인 메뉴로 오류가 심각하여 잠깐 비활성화되었습니다.
~~w!예약매수 [심볼] [주식수] [가격] - 지정가 매수 주문을 예약합니다.~~
~~w!예약매도 [심볼] [주식수] [가격] - 지정가 매도 주문을 예약합니다.~~
~~- 예약 매수/매도시 참고 : [가격]은 소숫점 2자리까지의 미화(달러)만 받습니다.~~ 
~~w!예약확인 - 예약된 주문을 확인합니다. (ID를 여기서 확인 가능)~~
~~w!예약취소 [주문ID] - 예약된 주문을 취소합니다.~~
w!리더보드 - 수익률 리더보드를 확인합니다.
"""
    await ctx.reply(embed=discord.Embed(description=help_text, color=discord.Color.blue()))

# 예외 처리
@bot.event
async def on_command_error(ctx, exception: Exception):  
    await ctx.send(embed=discord.Embed(title="에러가 발생했습니다!", description=str(exception)))


async def start_tasks():
    await bot.wait_until_ready()  # 봇이 준비될 때까지 대기

# 이벤트 루프 생성 및 비동기 작업 실행
loop = asyncio.get_event_loop()

# start_tasks를 이벤트 루프에서 실행
loop.create_task(start_tasks())

# 초기 설정
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        balance REAL,
        total_bonus REAL DEFAULT 0,
        initial_balance REAL,
        last_bonus_time TEXT
    )
    """)
    conn.commit()

bot.run('Your Bot Token')
