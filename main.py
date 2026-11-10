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
QUALITY_UP_ITERATIONS = 50  # 新規作成時の限界まで回す設定
MAX_UPDATE_LOOPS = 30       # 既存コード修正時の最大ループ回数（達成度ベース）
MAX_BUGFIX_LOOPS = 12       # バグ修正の最大ループ回数

# システムプロンプト
SYSTEM_INSTRUCTION = """あなたは世界的な天才フロントエンドエンジニア兼ゲームクリエイターです。
出力するHTML5ゲームは以下の要件を必ず満たしてください：
- モダンで洗練されたCSSデザイン（アニメーション、グラデーション、影の活用）
- スムーズなアニメーション（requestAnimationFrameの活用）
- ユーザーを飽きさせないエフェクトとフィードバック
- スマホとPC両方で遊べるレスポンシブデザイン
- 【重要】要素を無秩序に詰め込む「カオス化」を避け、引き算の美学を意識した統一感のあるUI/UXを維持すること。"""

# --- 1. 初期設定 ---
client = genai.Client()  # 環境変数 GEMINI_API_KEY が自動で読み込まれます

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

# --- 1.5. HTMLの健全性チェック関数（強化版） ---
def is_valid_html(code, min_length=500):
    """パッチマーカーの混入や省略がなく、HTMLとして完結しているか検証する"""
    if not code or len(code) < min_length:
        return False
    # 行頭に4連続以上の <, =, > が残っている場合はパッチ崩れ・コンフリクトと判定
    if re.search(r'^[ \t]*(?:<{4,}|={4,}|>{4,})', code, re.MULTILINE):
        return False
    # AIによる「省略コメント」が含まれている場合は不完全とみなす
    if re.search(r'(?:省略|中略|既存コード|rest of the code|existing code|unchanged)', code, re.IGNORECASE):
        return False
    # 開始タグと終了タグの両方が存在するか確認
    has_start = re.search(r'<!DOCTYPE\s+html|<html', code, re.IGNORECASE)
    has_end = re.search(r'</html>', code, re.IGNORECASE)
    return bool(has_start and has_end)

# --- 2. API呼び出し関数（リトライ・指数バックオフ・除外・タイムアウト機能付き） ---
def call_gemini_api(prompt_text, max_retries=5, extract_html=True):
    global ACTIVE_MODELS
    
    if not ACTIVE_MODELS:
        raise RuntimeError("API_LIMIT_REACHED")
        
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
                
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(api_request)
                    try:
                        response = future.result(timeout=120)  # 2分でタイムアウト
                    except concurrent.futures.TimeoutError:
                        raise TimeoutError("APIレスポンスが無応答のためタイムアウトしました")
                
                text = response.text or ""
                if not extract_html:
                    return text.strip()

                match = re.search(r'```html\s*\n(.*?)\n```', text, re.DOTALL)
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
                
                if not is_valid_html(code):
                    raise ValueError("生成されたHTMLが不完全（途中切れ・省略・マーカー混入）です")
                
                return code

            except TimeoutError as e:
                print(f"⚠️ [{model_name}] {e}。次のモデルへ...")
                break
                
            except errors.APIError as e:
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

    raise RuntimeError("API_LIMIT_REACHED")

# --- 2.5. 中間Push関数（コード消滅ガード・タイムアウト対策付き） ---
def save_and_push(html_code, commit_msg):
    if not is_valid_html(html_code):
        print("⚠️ 保存しようとしたHTMLが不完全なため、ファイル書き込みとPushを中止しました。")
        return

    # 既存ファイルがある場合、極端なサイズ縮小（コード消滅）を防止する
    if os.path.exists("index.html"):
        try:
            with open("index.html", "r", encoding="utf-8") as f:
                current_disk_code = f.read()
            if len(current_disk_code) > 1000 and len(html_code) < len(current_disk_code) * 0.5:
                print(f"⚠️ ファイルサイズが急激に減少（{len(current_disk_code)} -> {len(html_code)}文字）したため、上書き保存を中止しました。")
                return
        except Exception:
            pass

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_code)
    try:
        subprocess.run(["git", "config", "--global", "user.name", "github-actions[bot]"], check=True, timeout=10)
        subprocess.run(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True, timeout=10)
        subprocess.run(["git", "add", "index.html"], check=True, timeout=10)
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10)
        
        if "index.html" in status.stdout:
            subprocess.run(["git", "commit", "-m", commit_msg], check=True, timeout=10)
            try:
                subprocess.run(["git", "pull", "--rebase", "-X", "theirs", "origin", "main"], check=False, timeout=30)
                subprocess.run(["git", "push", "origin", "HEAD:main"], check=True, timeout=30)
                print(f"⬆️ GitHubへ中間Push完了: {commit_msg}")
            except subprocess.TimeoutExpired:
                print("⚠️ Git Push/Pull処理がタイムアウトしました。処理を継続します。")
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

# --- 3.5. パッチ適用関数（巻き込み防止・文字数比率ガード強化版） ---
def apply_patches(original_code, patch_text):
    raw_blocks = re.split(r'^[ \t]*<{4,}[ \t]*\r?\n', patch_text, flags=re.MULTILINE)
    
    valid_patches = []
    for block in raw_blocks[1:]:
        end_match = re.search(r'\r?\n^[ \t]*>{4,}[ \t]*(?:\r?\n|$)', block, flags=re.MULTILINE)
        if not end_match:
            print("⚠️ 終了マーカー(>>>>)が欠落している不完全なパッチブロックを破棄しました。")
            continue
            
        block_content = block[:end_match.start()]
        parts = re.split(r'\r?\n^[ \t]*={4,}[ \t]*\r?\n', block_content, flags=re.MULTILINE)
        if len(parts) != 2:
            print("⚠️ 区切りマーカー(====)の数が不正なパッチブロックを破棄しました。")
            continue
            
        search_block, replace_block = parts[0], parts[1]
        
        if re.search(r'^[ \t]*(?:<{4,}|={4,}|>{4,})', search_block + "\n" + replace_block, re.MULTILINE):
            print("⚠️ マーカーが内部に混入している異常なパッチブロックを破棄しました。")
            continue
            
        valid_patches.append((search_block, replace_block))

    # パッチが1つも抽出できなかった場合のフォールバック処理
    if not valid_patches:
        html_match = re.search(r'```html\s*\n(.*?)\n```', patch_text, re.DOTALL)
        if html_match:
            candidate = html_match.group(1).strip()
            # 完全なHTMLであり、かつ元のコードの70%以上の長さがある場合のみ全文適用を許可
            if is_valid_html(candidate) and len(candidate) >= len(original_code) * 0.7:
                print("⚠️ SEARCH/REPLACE形式が見つかりませんでしたが、完全なHTML全文を検出したため適用します。")
                return candidate
            else:
                print(f"⚠️ 抽出されたHTMLが不完全または短すぎるため（{len(candidate)}文字 / 元{len(original_code)}文字）、全文上書きを阻止しました。")
                return original_code
                
        print("⚠️ 有効なパッチも完全なHTMLも見つかりませんでした。変更をスキップします。")
        return original_code

    # パッチの適用
    new_code = original_code
    success_count = 0
    for search_block, replace_block in valid_patches:
        if search_block in new_code:
            new_code = new_code.replace(search_block, replace_block, 1)
            success_count += 1
        elif search_block.strip() and search_block.strip() in new_code:
            new_code = new_code.replace(search_block.strip(), replace_block.strip(), 1)
            success_count += 1
        else:
            print(f"⚠️ パッチ適用失敗（対象コードが見つかりません）:\n{search_block[:50]}...")

    # 最終防衛ライン：パッチ適用後のコードがHTMLとして壊れていないか、極端に短くなっていないか確認
    if not is_valid_html(new_code) or len(new_code) < len(original_code) * 0.5:
        print("❌ パッチ適用後のコードに構文崩れ・マーカー残存・または極端な文字数減少を検知したため、変更前にロールバックします。")
        return original_code

    print(f"✅ パッチ適用完了 ({success_count}/{len(valid_patches)} 個成功)")
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

以下の改善アイデアをすべて実装し、ゲームを進化させてください。
【改善アイデア】
{ideas}

【重要】コード全体を出力するのではなく、必ず以下の SEARCH/REPLACE 形式を使って変更部分のみを出力してください。
<<<<
置き換え対象の既存コード（一言一句違わずコピペすること）
====
新しく置き換えるコード
>>>>

`<thinking>` タグで設計思考を記述してから出力してください。
"""
            patch_text = call_gemini_api(prompt_apply, extract_html=False)
            new_html = apply_patches(html_content, patch_text)
            if new_html != html_content:
                html_content = new_html
                save_and_push(html_content, f"🚀 クオリティアップ ({i}/{QUALITY_UP_ITERATIONS}) (Issue #{issue_number})")
            else:
                print("⚠️ コードに変更がなかったため、保存をスキップします。")

        except RuntimeError as e:
            if "API_LIMIT_REACHED" in str(e):
                print("⚠️ 利用可能なすべてのモデルが上限に達したため、クオリティアップループを終了します。")
                break
        except Exception as e:
            print(f"⚠️ ループ {i} でエラーが発生しましたが継続します: {e}")

else:
    # --- 既存コード修正時: 達成度ベースの調整ループ ---
    print(f"\n--- 🔧 達成度ベース調整ループ開始（既存修正モード: 最大{MAX_UPDATE_LOOPS}回） ---")
    for i in range(1, MAX_UPDATE_LOOPS + 1):
        try:
            time.sleep(3)
            print(f"\n【調整ループ {i}/{MAX_UPDATE_LOOPS}】依頼の達成度チェック中...")
            prompt_check = f"""現在のコード:
```html
{html_content}
```
【ユーザーの依頼】
タイトル: {issue_title}
内容: {issue_body}

上記のコードがユーザーの依頼を100%完璧に満たしており、かつさらなるブラッシュアップの余地がない場合は「COMPLETED」とだけ出力してください。
まだ改善・追加・調整すべき点がある場合は、具体的な改善指示を3つ以内で簡潔に挙げてください（コードは書かないこと）。
"""
            check_result = call_gemini_api(prompt_check, extract_html=False)
            if "COMPLETED" in check_result.upper() and len(check_result) < 50:
                print("🎉 ユーザーの依頼が100%達成されたと判定されました！調整ループを完了します。")
                break

            print(f"📋 追加の改善ポイント:\n{check_result}\n")
            time.sleep(3)
            prompt_update = f"""現在のコード:
```html
{html_content}
```
以下の改善ポイントを実装してください：
{check_result}

【重要】必ず以下の SEARCH/REPLACE 形式を使って変更部分のみを出力してください。
<<<<
置き換え対象の既存コード（一言一句違わずコピペすること）
====
新しく置き換えるコード
>>>>
"""
            patch_text = call_gemini_api(prompt_update, extract_html=False)
            new_html = apply_patches(html_content, patch_text)
            if new_html != html_content:
                html_content = new_html
                save_and_push(html_content, f"🔧 依頼達成度アップ ({i}/{MAX_UPDATE_LOOPS}) (Issue #{issue_number})")

        except RuntimeError as e:
            if "API_LIMIT_REACHED" in str(e):
                print("⚠️ 利用可能なすべてのモデルが上限に達したため、調整ループを終了します。")
                break
        except Exception as e:
            print(f"⚠️ 調整ループ {i} でエラーが発生しましたが継続します: {e}")

# ========================================================
# フェーズ3: 自動テスト＆バグ修正ループ
# ========================================================
print(f"\n--- 🐛 フェーズ3: 自動テスト＆バグ修正ループ（最大{MAX_BUGFIX_LOOPS}回） ---")
for b in range(1, MAX_BUGFIX_LOOPS + 1):
    errors_found = test_game_code(html_content)
    if not errors_found:
        print("✅ エラーは検出されませんでした！すべての工程が完了しました。")
        break

    print(f"⚠️ エラー検出 ({b}/{MAX_BUGFIX_LOOPS}):\n" + "\n".join(errors_found))
    if not ACTIVE_MODELS:
        print("⚠️ 利用可能なモデルがないため、バグ修正を終了します。")
        break

    try:
        time.sleep(3)
        prompt_fix = f"""現在のコードで以下のJavaScriptエラーが検出されました。
【検出されたエラー】
{chr(10).join(errors_found)}

【現在のコード】
```html
{html_content}
```

エラーの原因を特定し、必ず以下の SEARCH/REPLACE 形式を使って修正パッチのみを出力してください。
<<<<
置き換え対象の既存コード（一言一句違わずコピペすること）
====
新しく置き換えるコード
>>>>
"""
        patch_text = call_gemini_api(prompt_fix, extract_html=False)
        new_html = apply_patches(html_content, patch_text)
        if new_html != html_content:
            html_content = new_html
            save_and_push(html_content, f"🐛 バグ自動修正 ({b}/{MAX_BUGFIX_LOOPS}) (Issue #{issue_number})")
    except RuntimeError as e:
        if "API_LIMIT_REACHED" in str(e):
            print("⚠️ API上限に達したためバグ修正ループを終了します。")
            break
    except Exception as e:
        print(f"⚠️ バグ修正中にエラーが発生しました: {e}")
        break

print(f"🏁 全プロセス完了 (Issue #{issue_number})")
