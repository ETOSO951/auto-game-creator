import os
import json
import subprocess
import time
import random
import re
import concurrent.futures
from google import genai
from google.genai import types
from google.genai import errors
from playwright.sync_api import sync_playwright

# ========================================================
# ⚙️ AIエージェントの設定
# ========================================================
QUALITY_UP_ITERATIONS = 30 # 新規作成時の限界まで回す設定
MAX_UPDATE_LOOPS = 30 # 既存コード修正時の最大ループ回数（達成度ベース）
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

# --- 2. API呼び出し関数（リトライ強化・指数バックオフ・除外・タイムアウト機能付き） ---
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
                def api_request():
                    return client.models.generate_content(
                        model=model_name,
                        contents=prompt_text,
                        config=types.GenerateContentConfig(
                            system_instruction=SYSTEM_INSTRUCTION
                        )
                    )
                
                # タイムアウト付きでAPIを呼び出す（無応答ハングアップ防止）
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(api_request)
                    try:
                        response = future.result(timeout=120) # 2分でタイムアウト（強制切断）
                    except concurrent.futures.TimeoutError:
                        raise TimeoutError("APIレスポンスが無応答のためタイムアウトしました")
                
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

            except TimeoutError as e:
                print(f"⚠️ [{model_name}] {e}。次のモデルへ...")
                break # タイムアウト時は次のモデルへ
                
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

# --- 2.5. 中間Push関数（タイムアウト対策付き） ---
def save_and_push(html_code, commit_msg):
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_code)
    try:
        subprocess.run(["git", "config", "--global", "user.name", "github-actions[bot]"], check=True, timeout=10)
        subprocess.run(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True, timeout=10)
        subprocess.run(["git", "add", "index.html"], check=True, timeout=10)
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10)
        
        if "index.html" in status.stdout:
            subprocess.run(["git", "commit", "-m", commit_msg], check=True, timeout=10)
            
            # コンフリクト時の対話プロンプトでハングアップするのを防ぐためタイムアウトを設定
            try:
                subprocess.run(["git", "pull", "--rebase", "-X", "theirs", "origin", "main"], check=False, timeout=30)
                subprocess.run(["git", "push", "origin", "HEAD:main"], check=True, timeout=30)
                print(f"⬆️ GitHubへ中間Push完了: {commit_msg}")
            except subprocess.TimeoutExpired:
                print("⚠️ Git Push/Pull処理がタイムアウトしました。競合が発生している可能性がありますが処理を継続します。")
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

# --- 3.5. パッチ適用関数（Diff形式での部分書き換え） ---
def apply_patches(original_code, patch_text):
    # <<<< から ====、そして >>>> までのブロックを抽出
    pattern = re.compile(r'<<<<\s*\n(.*?)\n====\s*\n(.*?)\n>>>>\s*', re.DOTALL)
    matches = pattern.findall(patch_text)
    
    if not matches:
        # パッチ形式が守られなかった場合のフォールバック（HTMLタグの抽出）
        html_match = re.search(r'```html\n(.*?)\n```', patch_text, re.DOTALL)
        if html_match:
            print("⚠️ SEARCH/REPLACE形式が見つかりませんでした。全文出力を適用します。")
            return html_match.group(1).strip()
        print("⚠️ パッチもHTMLコードも見つかりませんでした。変更をスキップします。")
        return original_code

    new_code = original_code
    success_count = 0
    for search_block, replace_block in matches:
        if search_block in new_code:
            # 完全一致での置換
            new_code = new_code.replace(search_block, replace_block)
            success_count += 1
        elif search_block.strip() in new_code:
            # 前後の空白による不一致を防ぐためのストリップ置換
            new_code = new_code.replace(search_block.strip(), replace_block.strip())
            success_count += 1
        else:
            print(f"⚠️ パッチ適用失敗（対象コードが見つかりません）:\n{search_block[:50]}...")
    
    print(f"✅ パッチ適用完了 ({success_count}/{len(matches)} 個成功)")
    return new_code

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
以下の既存コードに【追加・修正依頼】を適用してください。
【依頼】\nタイトル: {issue_title}\n内容: {issue_body}
【現在のコード】\n```html\n{existing_code}\n```

【重要】コード全体を出力するのではなく、必ず以下の SEARCH/REPLACE 形式を使って変更部分のみを出力してください。
新機能や変数を追加する場合は、追加したい場所の直前や直後の既存コードを SEARCH に含め、REPLACE に「既存コード＋追加コード」を書いてください。

<<<<
置き換え対象の既存コード（一言一句違わずコピペすること）
====
新しく置き換えるコード
>>>>

複数の箇所を変更する場合は、<<<< ==== >>>> のブロックを複数回使用してください。
`<thinking>` タグで設計思考を記述してから出力してください。
"""
        patch_text = call_gemini_api(prompt_p1, extract_html=False)
        html_content = apply_patches(existing_code, patch_text)
        
    # フェーズ1完了時点で一旦セーブ＆Push
    save_and_push(html_content, f"✨ フェーズ1 プロトタイプ作成完了 (Issue #{issue_number})")

except Exception as e:
    print(f"❌ フェーズ1で致命的なエラーが発生しました: {e}")
    exit(1)

# ========================================================
# フェーズ2: 自律的クオリティアップループ or 達成度ベース調整ループ
# ========================================================
if not existing_code:
    # --- 新規作成時: 計画的なクオリティアップ（%進行） ---
    print(f"\n--- 🌟 自律クオリティアップループ開始（新規作成モード: 最大{QUALITY_UP_ITERATIONS}回） ---")
    for i in range(1, QUALITY_UP_ITERATIONS + 1):
        try:
            time.sleep(3)
            print(f"\n【ループ {i}/{QUALITY_UP_ITERATIONS}】改善案の作成中...")
            
            # --- 段階的ディレクション（進捗に応じた計画的なプロンプト切り替え） ---
            progress_ratio = i / QUALITY_UP_ITERATIONS
            
            if progress_ratio < 0.3:
                phase_instruction = "【フェーズ: 基礎固め】 新しいゲームシステムや要素は一切追加せず、現在のゲームのコアメカニクス（操作性、当たり判定、基本ルール）を完璧にブラッシュアップし、コードを整理してください。"
            elif progress_ratio < 0.5:
                phase_instruction = "【フェーズ: 視覚・聴覚の洗練】 新要素は増やさず、既存の要素のCSSグラフィック、パーティクルエフェクト、アニメーションの滑らかさ、UIの美しさを徹底的に高めてリッチにしてください。"
            elif progress_ratio < 0.7:
                phase_instruction = "【フェーズ: ゲーム全体のボリューム増加】 これまで構築した基礎と視覚表現の上に、新しいステージ、敵の種類、アイテム、隠し要素、新しいギミックなど、ゲーム全体のボリュームと遊び応えを大幅に増加させる新要素を大胆に追加してください。"
            elif progress_ratio < 0.85:
                phase_instruction = "【フェーズ: UI整理と引き算の美学】 画面がごちゃごちゃしている場合は不要な要素を削除・整理し、プレイヤーが直感的に遊べるように洗練させてください。要素を詰め込みすぎない「引き算の美学」を意識してバランス調整に専念してください。"
            else:
                phase_instruction = "【フェーズ: 最終仕上げ】 ゲーム全体の完成度を商業レベルに引き上げるため、タイトル画面やゲームオーバー画面の演出強化、細かいディテールの磨き上げを行ってください。"

            prompt_idea = f"現在のコード:\n```html\n{html_content}\n```\nあなたはプロのゲームディレクターです。\n{phase_instruction}\nこの方針に厳格に従い、現在のコードをより洗練させるための具体的な改善案を3つ文章で挙げてください。コードは書かないでください。"
            
            ideas = call_gemini_api(prompt_idea, extract_html=False)
            print(f"💡 改善アイデア:\n{ideas}\n")
            
            time.sleep(3)
            print(f"【ループ {i}/{QUALITY_UP_ITERATIONS}】改善アイデアの実装中...")
            prompt_apply = f"""現在のコード:
```html
{html_content}
```
【実装するアイデア】
{ideas}

【重要】コード全体を出力するのではなく、必ず以下の SEARCH/REPLACE 形式を使って変更・追加部分のみを出力してください。
新機能や変数を追加する場合は、追加したい場所の既存コードを SEARCH に含め、REPLACE に「既存コード＋追加コード」を書いてください。

<<<<
置き換え対象の既存コード（一言一句違わずコピペすること）
====
新しく置き換えるコード
>>>>

複数の箇所を変更する場合は、<<<< ==== >>>> のブロックを複数回使用してください。
`<thinking>` タグで論理的に記述後に出力してください。省略厳禁。
"""
            patch_text = call_gemini_api(prompt_apply, extract_html=False)
            html_content = apply_patches(html_content, patch_text)
            
            print(f"💾 ループ {i} の成果を保存・Push中...")
            save_and_push(html_content, f"🔄 自律改善ループ {i}/{QUALITY_UP_ITERATIONS} 完了")
            
        except Exception as e:
            if "API_LIMIT_REACHED" in str(e):
                print(f"\n⚠️ トークン上限・API制限に到達しました！\nループ {i-1} までの成果を保存して、自動的に次のステップへ進みます。")
            else:
                print(f"\n⚠️ 予期せぬエラー発生: {e}\nループ {i-1} までの成果を保存して、自動的に次のステップへ進みます。")
            break

else:
    # --- 既存コード修正時: 達成度ベースの調整ループ ---
    print(f"\n--- 🎯 ユーザー要望達成ループ開始（既存編集モード: 最大{MAX_UPDATE_LOOPS}回） ---")
    for i in range(1, MAX_UPDATE_LOOPS + 1):
        try:
            time.sleep(3)
            print(f"\n【ループ {i}/{MAX_UPDATE_LOOPS}】達成度の評価と改善案の作成中...")
            
            prompt_eval = f"""現在のコード:
```html
{html_content}
```
【ユーザーからの依頼内容】
タイトル: {issue_title}
内容: {issue_body}

あなたは世界で最も厳格なコードレビュアーであり、粗探しをするテスターです。
【重要】あなたは上記のコードを書いていません。他人が書いたコードを審査しています。
現在のコードが「ユーザーの依頼」をどれくらい完璧に満たしているか、達成度を0〜100の数値で評価してください。

【評価の厳格なルール】
1. 依頼された機能が「すべて」実装され、かつ「正常に動作しそうか」をコードを読んで疑ってかかってください。
2. 見た目だけでなく、ロジック（JavaScript）のバグ、当たり判定の抜け漏れ、UIの崩れ落ちがないか厳格に探してください。
3. 少しでも機能が欠けている、またはバグの可能性がある場合は、絶対に100点をつけてはいけません（最高でも90点以下にすること）。
4. 「修正しました」という言葉を信じず、実際のコードのロジックに修正が反映されているか確認してください。

出力フォーマット（必ず以下の形式を厳守すること）:
達成度: [0〜100の数値]
改善案: [達成度が100でない場合、次にどのようなコード修正や微調整を行うべきか具体的な指示を書いてください。100の場合は「要望は完全に満たされています」と書いてください。]
"""
            eval_result = call_gemini_api(prompt_eval, extract_html=False)
            print(f"📊 評価結果:\n{eval_result}\n")
            
            # 達成度の抽出
            score = 0
            match = re.search(r'達成度[:：]\s*(\d+)', eval_result)
            if match:
                score = int(match.group(1))
                if score >= 100:
                    print("✨ 達成度100%に到達しました！ユーザーの要望は完全に反映されたため、調整ループを終了します。")
                    break
            else:
                print("⚠️ 達成度の数値が抽出できませんでした。処理を継続します。")
                
            time.sleep(3)
            print(f"【ループ {i}/{MAX_UPDATE_LOOPS}】改善案（微調整）の実装中...")
            prompt_apply = f"""現在のコード:
```html
{html_content}
```
【ユーザーの依頼内容】
{issue_body}
【実装する改善案】
{eval_result}
            
【重要】コード全体を出力するのではなく、必ず以下の SEARCH/REPLACE 形式を使って変更・追加部分のみを出力してください。
新機能や変数を追加する場合は、追加したい場所の既存コードを SEARCH に含め、REPLACE に「既存コード＋追加コード」を書いてください。

<<<<
置き換え対象の既存コード（一言一句違わずコピペすること）
====
新しく置き換えるコード
>>>>

複数の箇所を変更する場合は、<<<< ==== >>>> のブロックを複数回使用してください。
`<thinking>` タグで論理的に記述後に出力してください。省略厳禁。
"""
            patch_text = call_gemini_api(prompt_apply, extract_html=False)
            html_content = apply_patches(html_content, patch_text)
            
            print(f"💾 調整ループ {i} の成果を保存・Push中...")
            save_and_push(html_content, f"🔄 要望達成ループ {i}/{MAX_UPDATE_LOOPS} (達成度: {score}%) 完了")
            
        except Exception as e:
            if "API_LIMIT_REACHED" in str(e):
                print(f"\n⚠️ トークン上限・API制限に到達しました！\nループ {i-1} までの成果を保存して、自動的に次のステップへ進みます。")
            else:
                print(f"\n⚠️ 予期せぬエラー発生: {e}\nループ {i-1} までの成果を保存して、自動的に次のステップへ進みます。")
            break

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
                prompt_fix = f"""以下のエラーが発生しました。
{error_text}
【エラーが出たコード】
```html
{final_code}
```

【重要】コード全体を出力するのではなく、以下の SEARCH/REPLACE 形式を使ってバグ修正部分のみを出力してください。

<<<<
バグの原因となっている既存コード（一言一句違わずコピペすること）
====
修正後のコード
>>>>

原因分析を `<thinking>` タグで行った後、上記の形式でパッチを出力してください。
"""
                patch_text = call_gemini_api(prompt_fix, extract_html=False)
                final_code = apply_patches(final_code, patch_text)
                
                # バグ修正後も中間セーブ＆Push
                save_and_push(final_code, f"🛠 バグ自己修正 試行 {attempt}/{MAX_BUGFIX_LOOPS}")
            except Exception as e:
                print(f"⚠️ バグ修正中にAPI制限に到達しました。直前のコードを維持して終了します。")
                break
        else:
            print("⚠️ 最大修正回数に達しました。現状のコードを保存します。")

# --- 7. 保存と最終Push ---
save_and_push(final_code, f"🎉 AI自律改善版 (限界突破完了 / 合格:{success}) Issue #{issue_number}")
