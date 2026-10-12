import os
import json
import subprocess
import google.generativeai as genai

# 1. GitHub ActionsからiPhoneで投稿したIssueの文章を取得する
event_path = os.environ.get('GITHUB_EVENT_PATH')
with open(event_path, 'r', encoding='utf-8') as f:
    event_data = json.load(f)

issue_title = event_data['issue']['title']
issue_body = event_data['issue']['body']
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
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-1.5-flash") # 速度が速いflashモデルを使用
response = model.generate_content(prompt)

html_content = response.text

# 3. AIの返答から余計な文字（```html などのマークダウン）を取り除く
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
subprocess.run(["git", "config", "--global", "user.name", "github-actions[bot]"], check=True)
subprocess.run(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
subprocess.run(["git", "add", "index.html"], check=True)
subprocess.run(["git", "commit", "-m", f"AIがゲームを作成: Issue #{issue_number}"], check=True)
subprocess.run(["git", "push"], check=True)

print("完了しました！")
