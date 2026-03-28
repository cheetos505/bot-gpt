import discord
from discord.ext import commands
from discord import app_commands, ui
import json
import os
import time
import aiohttp
import asyncio
import random
import datetime
from dotenv import load_dotenv

# --- 1. การตั้งค่าพื้นฐาน ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
CONFIG_FILE = "config.json"
PROMPT_FILE = "system-prompt.txt"

DEFAULT_DATA = {
    "language": "Thai",
    "model": "nvidia/nemotron-3-super-120b-a12b:free",
    "api_key": os.getenv("OPENROUTER_API_KEY") or "YOUR_KEY_HERE",
    "webhook_url": os.getenv("DISCORD_WEBHOOK_URL") or "",
    "allowed_channels": []
}

# --- 2. ฟังก์ชันจัดการไฟล์ (JSON & TXT) ---
def load_config():
    if not os.path.exists(CONFIG_FILE) or os.stat(CONFIG_FILE).st_size == 0:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_DATA, f, indent=4)
        return DEFAULT_DATA
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "allowed_channels" not in data: data["allowed_channels"] = []
            return data
    except:
        return DEFAULT_DATA

def save_config(data):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def load_system_prompt():
    """ดึงข้อความจากไฟล์ system-prompt.txt"""
    if os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return 

async def send_webhook_error(err_text):
    conf = load_config()
    webhook_url = conf.get("webhook_url", "")
    if not webhook_url:
        return

    payload = {
        "content": f"⚠️ [BOT ERROR] {err_text}",
        "username": "worm-gpt-error"
    }

    try:
        async with aiohttp.ClientSession() as session:
            await session.post(webhook_url, json=payload, timeout=10)
    except Exception:
        # ไม่ให้ระเบิดซ้ำ
        pass

# --- 3. ระบบ Discord UI (Buttons) ---
class WexceaView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="『 ตั้งค่าข้อมูล 』", style=discord.ButtonStyle.secondary, emoji="⚙️")
    async def setup_btn(self, interaction: discord.Interaction, button: ui.Button):
        conf = load_config()
        channels = ", ".join([f"<#{c}>" for c in conf['allowed_channels']]) if conf['allowed_channels'] else "ทุกช่อง"
        embed = discord.Embed(title="⚙️ รายละเอียดการตั้งค่า", color=0x2b2d31)
        embed.add_field(name="โมเดล", value=f"`{conf['model']}`", inline=False)
        embed.add_field(name="ช่องที่อนุญาต", value=channels, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

# --- 4. ตัวบอทและตรรกะ AI ---
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all() # เปิดทั้งหมดเพื่อความชัวร์
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print("✅ Sync Slash Commands เรียบร้อย")

bot = MyBot()

rate_limit_state = {
    "auth_failures": 0,
    "first_429_time": 0,
    "blocked_until": 0,
}

async def ask_ai(prompt):
    conf = load_config()
    sys_prompt = load_system_prompt() or ""

    headers = {"Authorization": f"Bearer {conf['api_key']}", "Content-Type": "application/json"}
    data = {
        "model": conf["model"],
        "messages": [
            {"role": "system", "content": sys_prompt}, # ⬅️ อ่านจากไฟล์ system-prompt.txt
            {"role": "user", "content": prompt}
        ]
    }

    now_ts = time.time()
    if rate_limit_state["blocked_until"] > now_ts:
        remaining = int(rate_limit_state["blocked_until"] - now_ts)
        return f"❌ API Rate Limited: ชั่วคราว block {remaining} วินาที (ลองใหม่ภายหลัง)"

    max_retries = 8
    for attempt in range(1, max_retries + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data) as resp:
                    if resp.status == 200:
                        rate_limit_state['auth_failures'] = 0
                        rate_limit_state['first_429_time'] = 0
                        rate_limit_state['blocked_until'] = 0
                        res_json = await resp.json()
                        return res_json['choices'][0]['message']['content']

                    if resp.status == 429:
                        retry_after = resp.headers.get('Retry-After')
                        wait = None
                        if retry_after:
                            # Retry-After can be seconds or HTTP-date
                            try:
                                wait = float(retry_after)
                            except ValueError:
                                try:
                                    from email.utils import parsedate_to_datetime
                                    dt = parsedate_to_datetime(retry_after)
                                    wait = max((dt - datetime.datetime.now(dt.tzinfo)).total_seconds(), 0)
                                except Exception:
                                    wait = None
                        if wait is None:
                            wait = min(2 ** attempt, 60)
                        jitter = random.uniform(0.5, 1.3)
                        wait = min(wait * jitter, 120)

                        print(f"⚠️ 429 received (attempt {attempt}/{max_retries}), Retry-After={retry_after}, sleep={wait:.1f}s")

                        now_ts = time.time()
                        window = 60
                        if rate_limit_state["first_429_time"] == 0 or now_ts - rate_limit_state["first_429_time"] > window:
                            rate_limit_state["first_429_time"] = now_ts
                            rate_limit_state["auth_failures"] = 1
                        else:
                            rate_limit_state["auth_failures"] += 1

                        if rate_limit_state["auth_failures"] >= 3:
                            rate_limit_state["blocked_until"] = now_ts + 120
                            print(f"❌ APILimit breaker ON: blocked until {rate_limit_state['blocked_until']} (now {now_ts})")

                        if attempt < max_retries:
                            await asyncio.sleep(wait)
                            continue

                        return f"❌ API Error 429: Rate limited after {attempt} attempts (Retry-After={retry_after})"
                    if 500 <= resp.status < 600 and attempt < max_retries:
                        backoff = min(2 ** attempt, 30)
                        jitter = random.uniform(0.5, 1.2)
                        wait = backoff * jitter
                        print(f"⚠️ {resp.status} server error, retry {attempt}/{max_retries} in {wait:.1f}s")
                        await asyncio.sleep(wait)
                        continue

                    text = await resp.text()
                    return f"❌ API Error: {resp.status} - {text[:500]}"

        except aiohttp.ClientError as e:
            if attempt < max_retries:
                backoff = min(2 ** attempt, 30)
                jitter = random.uniform(0.5, 1.2)
                wait = backoff * jitter
                print(f"⚠️ Network error {e}, retry {attempt}/{max_retries} in {wait:.1f}s")
                await asyncio.sleep(wait)
                continue
            return f"❌ Network Error: {str(e)}"
        except Exception as e:
            return f"❌ Error: {str(e)}"

    return "❌ Error: ไม่สามารถติดต่อ API ได้ (ลองใหม่อีกครั้ง)"
# --- 5. คำสั่งและอีเวนต์ ---
@bot.tree.command(name="menu", description="แสดงเมนูแบบในรูปภาพ")
async def menu(interaction: discord.Interaction):
    embed = discord.Embed(title="💻 worm-gpt by 𝐷𝐸𝐿𝐸𝑇𝐸,cheetos2012", color=0x2b2d31)
    embed.description = (
        "╭── • 🟣 **worm-gpt 24/7**\n"
        "╰── • 🟣 **แจ้งเตือนผ่าน Webhook**\n"
    )
    # ใส่ URL รูปปราสาทของคุณที่นี่
    embed.set_image(url="https://tenor.com/view/so-good-wink-smile-oh-yeah-its-true-gif-24163876") 
    embed.set_footer(text="worm-gpt by 𝐷𝐸𝐿𝐸𝑇𝐸,cheetos2012")
    await interaction.response.send_message(embed=embed, view=WexceaView())

@bot.event
async def on_message(message):
    if message.author.bot: return
    
    conf = load_config()
    # ตรวจสอบช่องแชท
    if conf['allowed_channels'] and message.channel.id not in conf['allowed_channels']:
        return

    # ตอบเมื่อถูก Mention
    if bot.user.mentioned_in(message):
        clean_text = message.content.replace(f'<@{bot.user.id}>', '').strip()
        async with message.channel.typing():
            res = await ask_ai(clean_text)

            if str(res).startswith("❌"):
                await send_webhook_error(str(res))

            # --- ระบบตัดแบ่งข้อความป้องกัน Error 400 ---
            if len(res) > 2000:
                chunks = [res[i:i+1900] for i in range(0, len(res), 1900)]
                for chunk in chunks:
                    await message.reply(chunk)
            else:
                await message.reply(res)

# --- 6. เมนูใน TERMINAL (รันก่อนบอท) ---
def terminal_menu():
    while True:
        config = load_config()
        os.system('cls' if os.name == 'nt' else 'clear')
        print("\033[94m[ Main Menu ]\033[0m")
        print(f"1. \033[93mLanguage:\033[0m \033[92m{config['language']}\033[0m")
        print(f"2. \033[93mModel:\033[0m \033[92m{config['model']}\033[0m")
        print(f"3. \033[93mAllowed Channels:\033[0m \033[92m{config['allowed_channels']}\033[0m")
        print(f"4. \033[93mSet API Key\033[0m")
        print(f"5. \033[93mSet Webhook URL\033[0m")
        print(f"6. \033[92mStart Bot (Discord)\033[0m")
        print(f"7. Exit")
        
        choice = input("\nSelect: ")
        if choice == '1':
            config['language'] = input("Language Name: ")
            save_config(config)
        elif choice == '2':
            config['model'] = input("Model ID: ")
            save_config(config)
        elif choice == '3':
            cid = input("Enter Channel ID to add: ")
            if cid.isdigit():
                config['allowed_channels'].append(int(cid))
                save_config(config)
        elif choice == '4':
            config['api_key'] = input("Paste API Key: ")
            save_config(config)
        elif choice == '5':
            config['webhook_url'] = input("Paste Webhook URL: ")
            save_config(config)
        elif choice == '6':
            print("🚀 บอทกำลังเริ่มทำงาน...")
            bot.run(TOKEN)
            break
        elif choice == '7':
            exit()

if __name__ == "__main__":
    terminal_menu()