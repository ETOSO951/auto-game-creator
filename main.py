import os
import json
import subprocess
import time
import random
import re
import shutil
import concurrent.futures
from google import genai
from google.genai import types
from google.genai import errors
from playwright.sync_api import sync_playwright

# ========================================================
# ⚙️ AIエージェントの設定
# ========================================================
QUALITY_UP_ITERATIONS = 30  # クオリティアップループの最大回数
MAX_RETRIES = 5

SYSTEM_INSTRUCTION = """あなたは世界的な天才フロントエンドエンジニア兼ゲームクリエイターです。
出力するHTML5ゲームは以下の要件を必ず満たしてください：
- モダンで洗練されたCSSデザイン（アニメーション、グラデーション、影の活用）
- スムーズなアニメーション（requestAnimationFrameの活用）
- ユーザーを飽きさせないエフェクトとフィードバック
- スマホとPC両方で遊べるレスポンシブデザイン
- 【重要】要素を無秩序に詰め込む「カオス化」を避け、引き算の美学を意識した統一感のあるUI/UXを維持すること。
- コードを出力する際は、省略せずに必ず <!DOCTYPE html> から </html> までの一連の完全なコードを ```html ... ``` で囲んで出力してください。"""

# --- 1. 初期設定 ---
client = genai.Client()  # 環境変数 GEMINI_API_KEY が自動で読み込まれます

event_path = os.environ.get('GITHUB_EVENT_PATH')
issue_title = "無題のゲーム"
issue_body = ""

if event_path and os.path.exists(event_path):
    with open(event_path, 'r', encoding='utf-8') as f:
        event_data = json.load(f)
    issue_title = event_data.get('issue', {}).get('title', '無題のゲーム')
    issue_body = event_data.get('issue', {}).get('body') or ""

ACTIVE_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
]

# --- 1.5. HTMLの健全性チェック関数 ---
def is_complete_html(code):
    """HTMLとして最低限の開始タグと終了タグが揃っているか検証する"""
    if not code or len(code) < 100:
        return False
    # パッチマーカーが混入していないか確認
    if re.search(r'^[ \t]*(?:<{4,}|={4,}|>{4,})', code, re.MULTILINE):
        return False
    has_html = bool(re.search(r'<html[\s>]', code, re.IGNORECASE))
    has_end = bool(re.search(r'</html>', code, re.IGNORECASE))
    return has_html and has_end

# --- 2. API呼び出し関数 ---
def call_gemini_api(prompt_text):
    global ACTIVE_MODELS
    if not ACTIVE_MODELS:
        raise RuntimeError("API_LIMIT_REACHED")
        
    for model_name in list(ACTIVE_MODELS):
        print(f"🤖 [{model_name}] と通信中...")
        wait_time = 2.0 

        for attempt in range(MAX_RETRIES):
            try:
                def api_request():
                    return client.models.generate_content(
                        model=model_name,
                        contents=prompt_text,
                        config=types.GenerateContentConfig(
                            system_instruction=SYSTEM_INSTRUCTION
                        )
                    )
                
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(api_request)
                    try:
                        response = future.result(timeout=120)
                    except concurrent.futures.TimeoutError:
                        raise TimeoutError("APIレスポンスが無応答のためタイムアウトしました")
                
                text = response.text
                
                # HTMLコードの抽出
                match = re.search(r'```html\s*\n(.*?)\n```', text, re.DOTALL)
                if match:
                    code = match.group(1).strip()
                else:
                    match_any = re.search(r'```(?:.*?)\n(.*?)\n```', text, re.DOTALL)
                    code = match_any.group(1).strip() if match_any else text.strip()
                
                html_match = re.search(r'(<!DOCTYPE html>.*?</html>|<html>.*?</html>)', code, re.IGNORECASE | re.DOTALL)
                if html_match:
                    code = html_match.group(1).strip()
                
                if not is_complete_html(code):
                    raise ValueError("生成されたHTMLが不完全です")
                
                return code

            except TimeoutError as e:
                print(f"⚠️ [{model_name}] {e}。次のモデルへ...")
                break
                
            except errors.APIError as e:
                if e.code == 429:
                    if attempt == MAX_RETRIES - 1:
                        print(f"⚠️ [{model_name}] 上限到達(429)。除外します。")
                        if model_name in ACTIVE_MODELS:
                            ACTIVE_MODELS.remove(model_name)
                        break
                    sleep_time = wait_time + random.uniform(0, 1.0)
                    time.sleep(sleep_time)
                    wait_time *= 2.0
                else:
                    break
            except Exception as e:
                print(f"⚠️ [{model_name}] エラー: {e}")
                break

    raise RuntimeError("API_LIMIT_REACHED")

# --- 2.5. 安全なファイル保存とGit Push関数 ---
def save_and_push(html_code, commit_msg):
    if not is_complete_html(html_code):
        print("⚠️ 不完全なHTMLのため保存を中止しました。")
        return False

    # バックアップ作成
    if os.path.exists("index.html"):
        shutil.copy("index.html", "index.html.bak")

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_code)
        
    try:
        subprocess.run(["git", "config", "--global", "user.name", "github-actions[bot]"], check=True, timeout=10)
        subprocess.run(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True, timeout=10)
        subprocess.run(["git", "add", "index.html"], check=True, timeout=10)
        
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10)
        if "index.html" in status.stdout:
            subprocess.run(["git", "commit", "-m", commit_msg], check=True, timeout=10)
            subprocess.run(["git", "pull", "--rebase", "-X", "theirs", "origin", "main"], check=False, timeout=30)
            subprocess.run(["git", "push", "origin", "HEAD:main"], check=True, timeout=30)
            print(f"⬆️ GitHubへPush完了: {commit_msg}")
        return True
    except Exception as e:
        print(f"⚠️ Push失敗: {e}")
        return False

# --- 3. メイン処理（フェーズ1: 作成または改善） ---
existing_code = ""
if os.path.exists("index.html"):
    with open("index.html", "r", encoding="utf-8") as f:
        existing_code = f.read()

try:
    if not existing_code:
        print("【フェーズ1】ゲームの新規作成開始")
        prompt = f"""
以下の指示に従って、ブラウザで遊べる高品質なゲームを完全なHTMLとして作成してください。
タイトル: {issue_title}
内容: {issue_body}
"""
    else:
        print("【フェーズ1】既存ゲームの改善・機能追加")
        prompt = f"""
以下の既存のゲームコードに対し、新しい指示を反映させた【完全なコード全体】を最初から最後まで出力してください。一部の省略やパッチ形式は禁止です。
【追加・修正指示】
タイトル: {issue_title}
内容: {issue_body}

【現在のコード】
```html
{existing_code}
