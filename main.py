import os
import json
import subprocess
import google.generativeai as genai

# APIキーの存在チェック（設定されていなければここで止まるようにします）
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise RuntimeError("【エラー】GEMINI_API_KEY が設定されていません。GitHubのSecretsを確認してください。")

# 1. GitHub ActionsからiPhoneで投稿したIssueの文章を取得する
event_path = os.environ.get('GITHUB_EVENT_PATH')
if not event_path or not os.path.exists(event_path):
    raise RuntimeError("【エラー】GITHUB_EVENT_PATH が見つかりません。")

with open(event_path, 'r', encoding='utf-8') as f:
    event_data = json.load(f)

# 本文が空（None）の場合は空文字に変換する（エラー回避）
issue_title = event_data['issue'].get('title', '無題のゲーム')
issue_body = event_data['issue'].get('body') or "" 
issue_number = event_data['issue']['number']

# Geminiへの具体的な指示文を作る
prompt = f"""
タイトル: {issue_title}
内容: {issue_body}

上記の指示に従って、ブラウザで遊べるゲームを作ってください。
以下のルールを絶対に守ってください。
- HTML、CSS、JavaScriptをすべて1つのファイルにまとめること。
- 余計な説明文や挨拶は一切書かず、HTMLのコードだけを出力すること。
"""

print(f"Issue #{issue_number} を処理します: {issue_title}")

# 2. Gemini APIを呼び出してコードを生成する
try:
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash") 
    response = model.generate_content(prompt)
    html_content = response.text
except Exception as e:
    raise RuntimeError(f"【エラー】Gemini APIの呼び出しに失敗しました: {e}")

# 生成結果が空っぽの場合はエラーにする
if not html_content or html_content.strip() == "":
    raise RuntimeError("【エラー】Geminiからコードが生成されませんでした。")

# 3. AIの返答から余計な文字（```html などのマークダウン）を取り除く
html_content = html_content.strip()
if html_content.startswith("```html"):
    html_content = html_content.replace("```html", "", 1)
if html_content.startswith("```"):
    html_content = html_content.replace("```", "", 1)
if html_content.endswith("```"):
    html_content = html_content[:html_content.rfind("```")]

html_content = html_content.strip()

# 4. 生成されたコードを「index.html」という名前で保存する
with open("index.html", "w", encoding="utf-8") as f:
    f.write(html_content)

print("index.html を作成しました。GitHubへ保存します。")

# 5. 完成したファイルをGitHubのリポジトリに自動で上書き保存（コミット＆プッシュ）する
try:
    subprocess.run(["git", "config", "--global", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
    
    # 変更があったファイルだけを追加
    subprocess.run(["git", "add", "index.html"], check=True)
    
    # statusを確認し、変更がなければcommitしない（変更なしでcommitするとエラーになるため）
    status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    if "index.html" in status.stdout:
        subprocess.run(["git", "commit", "-m", f"AIがゲームを作成: Issue #{issue_number}"], check=True)
        # mainブランチに対して明示的にPushする
        subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
        print("完了しました！リポジトリに保存されました。")
    else:
        print("前回と同じコードが生成されたため、保存（コミット）をスキップしました。")

except subprocess.CalledProcessError as e:
    raise RuntimeError(f"【エラー】Gitの操作中にエラーが発生しました: {e}")
