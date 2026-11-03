import os

# جلب بيانات الأمان من GitHub Secrets
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# إعدادات الحساب وإدارة المخاطر لرأس مال 200$
TOTAL_CAPITAL = 200.0          # إجمالي رأس المال
MAX_CONTRACT_PRICE = 90.0      # الحد الأقصى لسعر العقد (90$ وتحت)
MIN_CONTRACT_PRICE = 10.0      # الحد الأدنى لسعر العقد (10$)

# وقف الخسارة وجني الأرباح
STOP_LOSS_PERCENT = 0.35       # وقف الخسارة عند 35% من قيمة العقد
TAKE_PROFIT_PERCENT = 0.70     # هدف الربح 70% من قيمة العقد
