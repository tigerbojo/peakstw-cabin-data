import sys
from pathlib import Path

# 讓測試從任何目錄執行都找得到 main.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
