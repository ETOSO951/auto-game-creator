import os
import json
import subprocess
import google.generativeai as genai
from tenacity import retry, stop_after_attempt, wait_exponential
from playwright.sync_api import sync_playwright
import re # 正規表現を使って強力にHTMLを抽出するために追加

# --- 1. 初期設定 ---
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise RuntimeError("【エラー】GEMINI_API_KEY が設定されていません。")
genai.configure(api_key=api_key)

event_path = os.environ.get('GITHUB_EVENT_PATH')
with open(event_path, 'r', encoding='utf-8') as f:
    event_data = json.load(f)

issue_title = event_data['issue'].get('title', '無題のゲーム')
issue_body = event_data['issue'].get('body') or "" 
issue_number = event_data['issue']['number']

# --- 2. AI呼び出し関数（強力なHTML抽出機能付き） ---
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def call_gemini_api(prompt_text, extract_html=True):
    print("🤖 Gemini APIと通信中...")
    model = genai.GenerativeModel("gemini-flash-latest")
    response = model.generate_content(prompt_text, request_options={"timeout": 300})
    
    if not extract_html:
        return response.text.strip()
        
    text = response.text
    # 正規表現で ```html と ``` の間にある中身だけを全て抜き出す
    match = re.search(r'```html\n(.*?)\n```', text, re.DOTALL)
    if match:
        code = match.group(1).strip()
    else:
        # htmlという指定がない ``` のみの場合
        match_any = re.search(r'```(?:.*?)\n(.*?)\n```', text, re.DOTALL)
        if match_any:
            code = match_any.group(1).strip()
        else:
            # マークダウン自体がない場合はそのまま使う
            code = text.strip()
            
    # AIが細切れに出力してしまっても、<html>から</html>までを強制抽出する
    html_match = re.search(r'(<!DOCTYPE html>.*?</html>|<html>.*?</html>)', code, re.IGNORECASE | re.DOTALL)
    if html_match:
        code = html_match.group(1).strip()

    return code

# --- 3. 自動テスト関数 ---
def test_game_code(html_code):
    print("🧪 自動テスト（エラー監視）を実行中...")
    with open("temp_test.html", "w", encoding="utf-8") as f:
        f.write(html_code)
    
    file_url = f"file://{os.path.abspath('temp_test.html')}"
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda err: errors.append(err.message))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        try:
            page.goto(file_url)
            page.wait_for_timeout(2000) 
        except Exception as e:
            errors.append(str(e))
        finally:
            browser.close()
            if os.path.exists("temp_test.html"):
                os.remove("temp_test.html")
    return errors

# --- 4. 既存コードの確認 ---
existing_code = ""
if os.path.exists("index.html"):
    with open("index.html", "r", encoding="utf-8") as f:
        existing_code = f.read()

html_content = ""

# ========================================================
# 5. 自律的クオリティアップループ
# ========================================================
if not existing_code:
    print(f"Issue #{issue_number}: 【フェーズ1】プロトタイプの作成")
    prompt_p1 = f"""
以下の指示に従って、ブラウザで遊べるゲームの「基礎的なプロトタイプ」を作ってください。
【指示】\nタイトル: {issue_title}\n内容: {issue_body}\n
ルール: 
1. HTML、CSS、JavaScriptを完全に1つのファイル(<!DOCTYPE html>から始まる)に統合してください。
2. 絶対にコードを複数ブロックに分割しないでください。
"""
    html_content = call_gemini_api(prompt_p1)
    
    print(f"Issue #{issue_number}: 【フェーズ2】AI自身による改善案の作成")
    prompt_p2 = f"""
あなたは天才ゲームクリエイターです。先ほど、以下のプロトタイプ版ゲームを作成しました。
【現在のコード】
```html
{html_content}
```
このゲームのクオリティを上げ、「もっと面白く」「もっとグラフィックを良く」するための具体的な追加アイデアを3つ挙げてください。
※コードは書かず、アイデアの文章だけを出力してください。
"""
    improvement_ideas = call_gemini_api(prompt_p2, extract_html=False)
    print(f"💡 AIの改善アイデア:\n{improvement_ideas}\n")
    
    print(f"Issue #{issue_number}: 【フェーズ3】クオリティアップ版（v2）の実装")
    prompt_p3 = f"""
以下のプロトタイプゲームに、あなたが提案した【改善アイデア】をすべて実装し、クオリティを上げた完成版のコードを出力してください。
【現在のコード】
```html
{html_content}
```
【実装する改善アイデア】
{improvement_ideas}

ルール: 
1. 途中で文章を挟まず、最初から最後まで1つの完全なHTMLコード(<!DOCTYPE html>〜</html>)として出力してください。
2. CSSもJSもすべて同じファイル内に記述し、絶対にコードを分割しないでください。
3. コード以外の説明文や挨拶は一切不要です。
"""
    html_content = call_gemini_api(prompt_p3)
else:
    print(f"Issue #{issue_number}: 【アップデート】既存コードの修正")
    prompt_update = f"""
以下のゲームコードに対して、【追加・修正依頼】を適用し、新しく動くコードを出力してください。
【追加・修正依頼】\nタイトル: {issue_title}\n内容: {issue_body}\n
【現在のコード】\n```html\n{existing_code}\n```
ルール: 
1. 最初から最後まで1つの完全なHTMLコードとして出力してください。コードを分割しないでください。
2. コード以外の説明文や挨拶は一切不要です。
"""
    html_content = call_gemini_api(prompt_update)

# ========================================================
# 6. 自己修正（バグ取り）ループ
# ========================================================
MAX_LOOPS = 3
final_code = ""
success = False

for attempt in range(1, MAX_LOOPS + 1):
    print(f"--- バグ修正ループ {attempt}/{MAX_LOOPS} ---")
    js_errors = test_game_code(html_content)
    
    if not js_errors:
        print("✅ テスト合格！バグはありません。")
        final_code = html_content
        success = True
        break
    else:
        print(f"❌ エラーを検知: {js_errors[:2]} (他略)")
        if attempt < MAX_LOOPS:
            print("🔄 エラーの自己修正を行います...")
            error_text = "\n".join(js_errors[:5])
            prompt_fix = f"""
作成したコードで以下のJavaScriptエラーが発生しました。修正した完全なコードを出力してください。
【エラー】\n{error_text}\n
【エラーが出たコード】\n```html\n{html_content}\n```
ルール: 
1. 最初から最後まで1つの完全なHTMLコードとして出力してください。コードを分割しないでください。
2. コード以外の説明文や挨拶は一切不要です。
"""
            html_content = call_gemini_api(prompt_fix)
        else:
            print("⚠️ 修正しきれませんでしたが、現状のコードを保存します。")
            final_code = html_content

# --- 7. 保存とPush ---
with open("index.html", "w", encoding="utf-8") as f:
    f.write(final_code)

try:
    subprocess.run(["git", "config", "--global", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", "index.html"], check=True)
    status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    
    if "index.html" in status.stdout:
        subprocess.run(["git", "commit", "-m", f"AI自律改善版 (合格:{success}) Issue #{issue_number}"], check=True)
        subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
        print("🎉 完了しました！")
    else:
        print("変更なしのためPushをスキップしました。")
except subprocess.CalledProcessError as e:
    raise RuntimeError(f"【エラー】Git操作失敗: {e}")
