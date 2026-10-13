import os
import json
import subprocess
import time
import random
import re
from google import genai
from google.genai import types
from google.genai import errors
from playwright.sync_api import sync_playwright

# ========================================================
# ⚙️ AIエージェントの設定
# ========================================================
QUALITY_UP_ITERATIONS = 10 # 限界まで回す設定
MAX_BUGFIX_LOOPS = 4

# システムプロンプト
SYSTEM_INSTRUCTION = """あなたは世界的な天才フロントエンドエンジニア兼ゲームクリエイターです。
出力するHTML5ゲームは以下の要件を必ず満たしてください：
- モダンで洗練されたCSSデザイン（アニメーション、グラデーション、影の活用）
- スムーズなアニメーション（requestAnimationFrameの活用）
- ユーザーを飽きさせないエフェクトとフィードバック
- スマホとPC両方で遊べるレスポンシブデザイン
- 【重要】要素を無秩序に詰め込む「カオス化」を避け、引き算の美学を意識した統一感のあるUI/UXを維持すること。"""

# --- 1. 初期設定 ---
client = genai.Client() # 環境変数 GEMINI_API_KEY が自動で読み込まれます

event_path = os.environ.get('GITHUB_EVENT_PATH')
with open(event_path, 'r', encoding='utf-8') as f:
    event_data = json.load(f)

issue_title = event_data['issue'].get('title', '無題のゲーム')
issue_body = event_data['issue'].get('body') or "" 
issue_number = event_data['issue']['number']

# --- グローバルモデルリスト（使えないモデルはここから削っていく） ---
ACTIVE_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-pro-preview"
]

# --- 2. API呼び出し関数（リトライ強化・指数バックオフ・除外機能付き） ---
def call_gemini_api(prompt_text, max_retries=5, extract_html=True):
    global ACTIVE_MODELS
    
    if not ACTIVE_MODELS:
        raise RuntimeError("API_LIMIT_REACHED")
        
    # リストのコピーを使ってループ（ループ中の要素削除エラーを防ぐため）
    for model_name in list(ACTIVE_MODELS):
        print(f"🤖 [{model_name}] と通信中...")
        wait_time = 2.0 

        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt_text,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION
                    )
                )
                
                text = response.text
                if not extract_html:
                    return text.strip()

                match = re.search(r'```html\n(.*?)\n```', text, re.DOTALL)
                if match:
                    code = match.group(1).strip()
                else:
                    match_any = re.search(r'```(?:.*?)\n(.*?)\n```', text, re.DOTALL)
                    if match_any:
                        code = match_any.group(1).strip()
                    else:
                        code = text.strip()
                html_match = re.search(r'(<!DOCTYPE html>.*?</html>|<html>.*?</html>)', code, re.IGNORECASE | re.DOTALL)
                if html_match:
                    code = html_match.group(1).strip()
                
                return code

            except errors.APIError as e:
                # 429エラー (Resource Exhausted / レート制限) 
                if e.code == 429:
                    if attempt == max_retries - 1:
                        print(f"⚠️ [{model_name}] で上限到達(429)。リストから除外して次のモデルへ...")
                        if model_name in ACTIVE_MODELS:
                            ACTIVE_MODELS.remove(model_name)
                        break
                    sleep_time = wait_time + random.uniform(0, 1.0)
                    print(f"⏳ レート制限到達(429)。{sleep_time:.2f}秒待機してリトライします (試行 {attempt + 1}/{max_retries})")
                    time.sleep(sleep_time)
                    wait_time *= 2.0 
                # 404や403など致命的なエラー
                elif e.code in [404, 403, 400]:
                    print(f"⚠️ [{model_name}] は利用不可(コード:{e.code})。リストから完全に除外します。")
                    if model_name in ACTIVE_MODELS:
                        ACTIVE_MODELS.remove(model_name)
                    break
                else:
                    print(f"⚠️ [{model_name}] APIエラー発生({e.code}): {e.message}。次のモデルへ...")
                    break 
                
            except Exception as e:
                print(f"⚠️ [{model_name}] 予期せぬエラー発生: {e}。次のモデルへ...")
                break 

    # すべてのモデル・リトライが失敗した場合は例外を投げて安全に抜けさせる
    raise RuntimeError("API_LIMIT_REACHED")

# --- 2.5. 中間Push関数 ---
def save_and_push(html_code, commit_msg):
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_code)
    try:
        subprocess.run(["git", "config", "--global", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", "index.html"], check=True)
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if "index.html" in status.stdout:
            subprocess.run(["git", "commit", "-m", commit_msg], check=True)
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
            subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
            print(f"⬆️ GitHubへ中間Push完了: {commit_msg}")
        else:
            print("変更なしのためPushスキップ")
    except Exception as e:
        print(f"⚠️ Push失敗(実行は継続します): {e}")

# --- 3. 自動テスト関数 ---
def test_game_code(html_code):
    print("🧪 自動テスト（エラー監視）を実行中...")
    with open("temp_test.html", "w", encoding="utf-8") as f:
        f.write(html_code)
    
    file_url = f"file://{os.path.abspath('temp_test.html')}"
    errors_list = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda err: errors_list.append(err.message))
        page.on("console", lambda msg: errors_list.append(msg.text) if msg.type == "error" else None)
        try:
            page.goto(file_url)
            page.wait_for_timeout(3000)
        except Exception as e:
            errors_list.append(str(e))
        finally:
            browser.close()
            if os.path.exists("temp_test.html"):
                os.remove("temp_test.html")
    return errors_list

# --- 4. 既存コードの確認 ---
existing_code = ""
if os.path.exists("index.html"):
    with open("index.html", "r", encoding="utf-8") as f:
        existing_code = f.read()

html_content = ""

# ========================================================
# フェーズ1: ユーザー指示の反映（新規作成 or 既存修正）
# ========================================================
try:
    if not existing_code:
        print(f"Issue #{issue_number}: 【フェーズ1】プロトタイプの作成（新規）")
        prompt_p1 = f"""
以下の指示に従って、ブラウザで遊べる高品質なゲームを作成してください。
【指示】\nタイトル: {issue_title}\n内容: {issue_body}
【必須実装要件】
1. スタート画面、ゲームオーバー画面、スコアUIを実装すること
ルール: 
1. `<thinking>` タグで設計思考を記述後、完全なHTMLコードを ```html ... ``` で出力してください。
"""
        html_content = call_gemini_api(prompt_p1)
    else:
        print(f"Issue #{issue_number}: 【フェーズ1】ユーザー指示の反映（既存コードの修正）")
        prompt_p1 = f"""
以下の既存コードに【追加・修正依頼】を適用し、動くコードを出力してください。
【依頼】\nタイトル: {issue_title}\n内容: {issue_body}
【現在のコード】\n```html\n{existing_code}\n```
ルール: `<thinking>` タグで設計思考を記述後、完全なHTMLコードを ```html ... ``` で出力してください。
"""
        html_content = call_gemini_api(prompt_p1)
        
    # フェーズ1完了時点で一旦セーブ＆Push
    save_and_push(html_content, f"✨ フェーズ1 プロトタイプ作成完了 (Issue #{issue_number})")

except Exception as e:
    print(f"❌ フェーズ1で致命的なエラーが発生しました: {e}")
    exit(1)

# ========================================================
# フェーズ2: 自律的クオリティアップループ（限界突破仕様）
# ========================================================
print(f"\n--- 🌟 自律クオリティアップループ開始（最大{QUALITY_UP_ITERATIONS}回） ---")
for i in range(1, QUALITY_UP_ITERATIONS + 1):
    try:
        time.sleep(3)
        print(f"\n【ループ {i}/{QUALITY_UP_ITERATIONS}】改善案の作成中...")
        
        # --- 段階的ディレクション（進捗に応じた計画的なプロンプト切り替え） ---
        progress_ratio = i / QUALITY_UP_ITERATIONS
        
        if progress_ratio < 0.3:
            phase_instruction = "【フェーズ: 基礎固め】 新しいゲームシステムや要素は一切追加せず、現在のゲームのコアメカニクス（操作性、当たり判定、基本ルール）を完璧にブラッシュアップし、コードを整理してください。"
        elif progress_ratio < 0.6:
            phase_instruction = "【フェーズ: 視覚・聴覚の洗練】 新要素は増やさず、既存の要素のCSSグラフィック、パーティクルエフェクト、アニメーションの滑らかさ、UIの美しさを徹底的に高めてリッチにしてください。"
        elif progress_ratio < 0.8:
            phase_instruction = "【フェーズ: UI整理と引き算の美学】 画面がごちゃごちゃしている場合は不要な要素を削除・整理し、プレイヤーが直感的に遊べるように洗練させてください。バランス調整に専念してください。"
        else:
            phase_instruction = "【フェーズ: 最終仕上げ】 ゲーム全体の完成度を商業レベルに引き上げるため、タイトル画面やゲームオーバー画面の演出強化、細かいディテールの磨き上げを行ってください。"

        prompt_idea = f"現在のコード:\n```html\n{html_content}\n```\nあなたはプロのゲームディレクターです。\n{phase_instruction}\nこの方針に厳格に従い、現在のコードをより洗練させるための具体的な改善案を3つ文章で挙げてください。コードは書かないでください。"
        
        # 修正: アイデア出しもリトライ・フォールバック関数（call_gemini_api）を経由させる
        ideas = call_gemini_api(prompt_idea, extract_html=False)
        print(f"💡 改善アイデア:\n{ideas}\n")
        
        time.sleep(3)
        print(f"【ループ {i}/{QUALITY_UP_ITERATIONS}】改善アイデアの実装中...")
        prompt_apply = f"現在のコード:\n```html\n{html_content}\n```\n【実装するアイデア】\n{ideas}\nルール: `<thinking>` タグで論理的に記述後、完全なHTMLコードを ```html ... ``` で出力してください。省略厳禁。"
        
        # 実装
        html_content = call_gemini_api(prompt_apply)
        
        # 💾 【重要】ループ途中の漸進的更新（毎ループGitへPushしてブラウザで確認可能に）
        print(f"💾 ループ {i} の成果を保存・Push中...")
        save_and_push(html_content, f"🔄 自律改善ループ {i}/{QUALITY_UP_ITERATIONS} 完了")
        
    except Exception as e:
        if "API_LIMIT_REACHED" in str(e):
            print(f"\n⚠️ トークン上限・API制限に到達しました！\nループ {i-1} までの成果を保存して、自動的に次のステップへ進みます。")
        else:
            print(f"\n⚠️ 予期せぬエラー発生: {e}\nループ {i-1} までの成果を保存して、自動的に次のステップへ進みます。")
        break # エラーが起きたらループを安全に脱出する

# ========================================================
# フェーズ3: 自己修正（バグ取り）ループ
# ========================================================
print(f"\n--- 🛠 バグ監視・自己修正ループ開始（最大{MAX_BUGFIX_LOOPS}回） ---")
final_code = html_content
success = False

for attempt in range(1, MAX_BUGFIX_LOOPS + 1):
    print(f"--- テスト実行 {attempt}/{MAX_BUGFIX_LOOPS} ---")
    js_errors = test_game_code(final_code)
    
    if not js_errors:
        print("✅ テスト合格！バグはありません。")
        success = True
        break
    else:
        print(f"❌ エラーを検知: {js_errors[:2]} (他略)")
        if attempt < MAX_BUGFIX_LOOPS:
            try:
                print("🔄 エラーの自己修正を行います...")
                time.sleep(3)
                error_text = "\n".join(js_errors[:5])
                prompt_fix = f"以下のエラーが発生しました。\n{error_text}\n【エラーが出たコード】\n```html\n{final_code}\n```\nルール: 原因分析後、完全なHTMLコードを出力してください。"
                
                final_code = call_gemini_api(prompt_fix)
                
                # バグ修正後も中間セーブ＆Push
                save_and_push(final_code, f"🛠 バグ自己修正 試行 {attempt}/{MAX_BUGFIX_LOOPS}")
            except Exception as e:
                print(f"⚠️ バグ修正中にAPI制限に到達しました。直前のコードを維持して終了します。")
                break
        else:
            print("⚠️ 最大修正回数に達しました。現状のコードを保存します。")

# --- 7. 保存と最終Push ---
save_and_push(final_code, f"🎉 AI自律改善版 (限界突破完了 / 合格:{success}) Issue #{issue_number}")
