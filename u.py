from openai import OpenAI
import streamlit as st
import pandas as pd 
# database.py から必要な関数をインポート
from database import get_db , get_all_ingredients, add_ingredient
from database import delete_ingredient, create_tables, update_ingredient, Settings, update_settings, update_ingredient_quantity
from database import ShoppingItem, add_shopping_item, get_all_shopping_items, delete_shopping_item
from database import RecipeHistory, add_recipe_history, get_all_recipe_history, delete_recipe_history,clear_shopping_list
from sqlalchemy.orm import Session
import random
import time
import datetime 
import io # 画像処理用にioをインポート
from PIL import Image # 画像処理用にPILをインポート
import base64 # GPT-4o Vision用にbase64をインポート
# 🚨 修正A: YAMLファイルの読み込みとAuthenticatorの初期化 🚨
import yaml
from yaml.loader import SafeLoader
import streamlit_authenticator as stauth
import re # 自動減算処理用にインポート



# YAMLファイルの読み込み
try:
    # 🚨 修正: encoding='utf-8' を追加 🚨
    with open('config.yaml',encoding='utf-8') as file:
        config = yaml.load(file, Loader=SafeLoader)
except Exception as e:
    st.error(f"認証設定ファイル (config.yaml) のロードエラー: {e}")
    config = None
    st.stop() # エラー時はここで停止

credentials = config['credentials']
cookie = config['cookie']

# Authenticatorの初期化
authenticator = stauth.Authenticate(
    credentials,
    cookie['name'], 
    cookie['key'], 
    cookie['expiry_days']
)

#自動減算処理用の関数
def parse_quantity_and_unit(input_str):
    """'1000ml' を 1000.0 と 'ml' に分ける関数"""
    match = re.match(r'([0-9.]+)\s*(\w+)?', str(input_str))
    if match:
        qty = float(match.group(1))
        unit = match.group(2) if match.group(2) else "個"
        return qty, unit
    return None, None

# ----------------------------------------------------
# 🚨 新規追加：レシート画像処理ロジック 🚨
# ----------------------------------------------------

def process_receipt(uploaded_file):
    """
    レシート画像をGPT-4o Vision APIに送信し、食材リストをJSONで抽出する。
    """
    try:
        # 1. OpenAI クライアントの初期化 (st.secrets["openai"]["api_key"] を使用)
        client = OpenAI(api_key=st.secrets["openai"]["api_key"]) 

        # 2. アップロードされたファイルをバイナリに変換し、Base64エンコード
        image_bytes = uploaded_file.getvalue()
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        
        # 3. システムプロンプト (抽出ルール)
        # (システムプロンプトは変更なしでOK)
        system_content = """
        あなたは優秀なデータ抽出AIです。提供されたレシート画像から、購入された品目名のみを正確に抽出してください。

        ################################################################
        ## 🚨 最重要指令：JSON出力の厳格な強制 🚨
        ################################################################
        1. **出力形式:** 抽出結果は、他の情報や説明（前置き、後書き、コメントなど）を一切加えず、以下の**JSON形式（Python辞書形式）のみ**で出力してください。
        2. **コードブロックの禁止:** Markdownのコードブロック（```json...```）は**絶対に使用しないでください**。

        【抽出ルール】
        1. **抽出対象:** 食材、生鮮品、調味料、飲料など、**冷蔵庫に入れるべき食品**および**調理に使うもの**のみを抽出する。
        2. **除外対象:** 合計金額、小計、消費税、店名、日付、時間、ポイント、**価格**、**日用品（洗剤、衣料品、文房具など）**は絶対に出力しないでください。
        3. **数量:** レシートから正確に数量を読み取ることは困難であるため、抽出した品目ごとに数量は常に「1」としてください。

        【出力形式】
        {
        "items": [
            {"name": "豚こま", "quantity": "1"},
            {"name": "牛乳", "quantity": "1"},
            {"name": "キャベツ", "quantity": "1"}
        ]
        }
        """
        
        # 4. API呼び出し (GPT-4o Visionを使用)
        response = client.chat.completions.create(
            model="gpt-4o", # 👈 モデル名をgpt-4oに固定
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": [
                    {"type": "text", "text": "レシート画像の内容を上記のルールに従ってJSONで抽出してください。"},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}"
                    }}
                ]}
            ],
            temperature=0.0
        )
        
        # 5. 結果のパース
        import json
        # GPTが出力したJSON文字列から不要なマークダウンを削除
        json_str = response.choices[0].message.content.strip().lstrip('```json').rstrip('```').strip()
        extracted_data = json.loads(json_str)
        
        return extracted_data.get("items", [])
        
    except Exception as e:
        st.error(f"🚨 レシート処理中にエラーが発生しました。\n詳細: {e}")
        return None


# フォームリセット用のキー初期化
if "registration_key" not in st.session_state:
    st.session_state["registration_key"]= str(random.randint(0,100000))
if "deletion_key" not in st.session_state:
    st.session_state["deletion_key"]= str(random.randint(0,100000))
if "update_key" not in st.session_state:
    st.session_state["update_key"] = str(random.randint(0,100000))

# 調整フォームのリセットキー
if "adjustment_form_key" not in st.session_state:
    st.session_state["adjustment_form_key"] = str(random.randint(0,100000))

# 提案結果保持用のキー
if "last_suggestion" not in st.session_state:
    st.session_state["last_suggestion"] = None # 提案結果を保存
if "proposal_warning" not in st.session_state:
    st.session_state["proposal_warning"] = False


#自動減算処理用の関数
def parse_ingredients_from_recipe(recipe_text):
    """
    レシピテキストから「食材名」「数値」「単位」を抽出する
    """
    results = []
    # 「食材名: 数量 単位」の形式を探す正規表現
    pattern = r"-\s*([^:]+):\s*([0-9.]+)\s*(\w+)"
    
    matches = re.findall(pattern, recipe_text)
    for name, qty, unit in matches:
        results.append({
            "name": name.strip(),
            "quantity": float(qty),
            "unit": unit.strip()
        })
    return results


def suggest_menu(ingredients_list, allergy_list, time_constraint = ""):
    """
    登録された食材リストに基づき、ChatGPT (OpenAI API) を使用して献立を提案する
    """
    if not ingredients_list:
        return "冷蔵庫が空です... 😢 まずは食材を登録してください！"
    
    ingredient_details = []
    # 期限が近い順にソート
    ingredients_list.sort(key=lambda item: item.use_by_date)
    
    for item in ingredients_list:
        date_str = item.use_by_date.strftime("%Y/%m/%d") if item.use_by_date else "期限なし"
        ingredient_details.append(
            f"- {item.name}: {item.quantity} {item.unit} (期限: {date_str})"
        )
    ingredients_text = "\n".join(ingredient_details)
    
    allergy_instruction = ""
    if allergy_list:
        allergy_str = "、".join(allergy_list)
        allergy_instruction = f"""
        【重要制約】
        以下の食材はアレルギーまたは除外対象です。
        提案するレシピにはこれらの食材（{allergy_str}）を絶対に使用しないでください。
        """
    
    # --------------------------------------------------------------------------
    # 【プロンプト改善１】システムプロンプトに制約を集約
    # --------------------------------------------------------------------------
    system_content_for_openai = f"""
    あなたは優秀な料理専門家であり、レシピ提案AIです。簡潔で実用的なレシピ提案を作成してください。
    
    {allergy_instruction}

    ################################################################
    ## 🚨 最重要指令：制約の厳守 🚨
    ################################################################

    # ... (このセクションは変更なしでOK) ...

    ################################################################
    ## 提案の形式
    ################################################################

    以下の手順と**セクションの番号と見出しを厳守**してください。**レシピ名以外では装飾的なMarkdown（#、##、***、太字など）を絶対に使用しないでください**。

    1. **提案名**：レシピ名を太字で魅力的に書く。（例：**絶品！鶏むね肉のやみつき炒め**）
    2. **調理法**：レシピの調理法（例：煮物、炒め物）を明記する。
    3. **調理時間**：合計時間と内訳（例：調理時間：35分（内訳：下準備10分、加熱25分））を明記する。
    4. **使用食材**：リストから使用する食材をすべて抽出する。
        - 必ず「食材名: 数量 単位」の形式で記載すること（例：たまねぎ: 0.5 個）。
        - 単位は、提示されたリストの単位を絶対に変更せずそのまま使用すること。
    5. **不足食材・買い物リスト**：提案レシピに必要な、冷蔵庫リストにない調味料や副材料を箇条書きでリストアップする。（**パースの安定性のために、4番と5番を連続させました**）
    6. **提案理由**：期限が近い食材に言及し、提案した理由を簡潔に述べる。
    7. **調理手順**：以下のルールに従い、**簡潔で具体的な「命令文」**を箇条書きで示す。
        - **最初のステップ**は、必ず「**下準備**（切る、漬けるなど）」とし、使用する**全食材の準備**を完了させること。
        - 各ステップは**明確な動詞**（例：切る、炒める、煮る、混ぜる）で終わらせ、**時間目安**を（例：[2分]）で追記すること。
        - **調味料を加えるタイミング**を明確に示すこと。
    """
    
    # --------------------------------------------------------------------------
    # ユーザープロンプトは簡潔に
    # --------------------------------------------------------------------------
    prompt = f"""
    【提案に使用する食材リスト】
    {ingredients_text}
    
    上記の食材リストをすべて使用して、システムプロンプトの形式を厳守した献立を提案してください。
    """
        
    try:
        # OpenAI APIキーはStreamlit Secretsから取得
        client = OpenAI(api_key=st.secrets["openai"]["api_key"]) 
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_content_for_openai}, 
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        
        return response.choices[0].message.content
    
    except Exception as e:
        return f"🚨 献立提案APIの呼び出し中にエラーが発生しました。\n原因: {e}"





def display_auto_clear_message(message: str, level: str):
    """
    メッセージを指定されたレベルで表示し、2秒後に自動的に消去する。
    """
    placeholder = st.empty()
    
    if level == "success":
        placeholder.success(message)
    elif level == "warning":
        placeholder.warning(message)
    elif level == "error":
        placeholder.error(message)
    
    # st.time.sleep は非推奨、標準の time.sleep を使用
    time.sleep(2)
    
    placeholder.empty()


def save_allergy_settings():
    """
    アレルギー設定をデータベースに保存する（on_change用）
    """
    with next(get_db()) as db:
        try:
            update_settings(db,st.session_state["allergy_input"])
        except Exception as e:
            st.error(f"アレルギー設定の保存中にエラーが発生しました: {e}")
            pass

# ログイン画面の表示
authenticator.login(location="main")

authentication_status = st.session_state.get("authentication_status")
name = st.session_state.get("name")
username = st.session_state.get("username")

# ログイン後のメイン処理
if authentication_status:
    # ここから、アプリのメイン画面のコードが始まります
    # (例: st.title("冷蔵庫の食材管理アプリ") など)
                # 🚨 ログアウトボタンを配置 🚨
                st.sidebar.write(f"ようこそ, {name} さん") 
                authenticator.logout('ログアウト', 'sidebar')

                def main():
                    st.title("献立提案アプリ") 
                    st.markdown("---")
                    
                    # 1. データベースの初期設定と設定読み込み
                    create_tables()
                    
                    # 設定の読み込み
                    try:
                        with next(get_db()) as db_settings:
                            setting_row = db_settings.query(Settings).filter(Settings.id == 1).first()
                            if setting_row and 'allergy_input' not in st.session_state:
                                st.session_state['allergy_input'] = setting_row.allergy_text
                    except Exception as e:
                        st.error(f"設定の読み込み中にエラーが発生しました: {e}")

                    
                    
                    # 3. アレルギー設定エリア
                    st.header("🚫 アレルギー・除外食材の設定")
                    st.markdown("メニュー提案に使用しない食材やアレルギー食材を入力してください。（改行区切り）")
                    
                    st.text_area(
                        "アレルギー、除外食材", placeholder="例: ピーナッツ\n例: えび\n例: 牛乳",
                        key="allergy_input",
                        on_change = save_allergy_settings 
                        )
                    # タブの作成と定義
                    tab_add_ingredients, tab_list_and_update_ingredients,tab_menu = st.tabs(
                        ["🥦 食材登録", "🍆食材リストの更新・変更","💡 献立提案"])    
                    
                    #食材登録タブ
                    with tab_add_ingredients:
                        # 4. 食材登録フォームエリア
                        st.header("➕ 新しい食材の登録")
                        st.markdown("献立提案のための食材を登録してください。")

                        with st.form(key=st.session_state['registration_key']):
                            new_name = st.text_input("食材名", placeholder="例: 鶏むね肉, 玉ねぎ")
                            new_quantity = st.text_input("数量・単位", placeholder="例: 200g, 1個")
                            
                            # 期限の初期値を今日に設定
                            new_use_by_date = st.date_input("賞味期限/消費期限", value=datetime.date.today())

                            submit_button = st.form_submit_button(label='食材を登録する 💾',type="primary")

                        if submit_button:
                            if new_name and new_quantity:
                                try:
                                    # 1. ここで「1000ml」を数値(1000.0)と単位("ml")に分解する
                                    parsed_qty, parsed_unit = parse_quantity_and_unit(new_quantity)
                                    
                                    if parsed_qty is None:
                                        st.error("数量は '200g' や '1個' のように、数字から入力してください。")
                                    else:
                                        final_date = new_use_by_date if isinstance(new_use_by_date, datetime.date) else None
                                        with next(get_db()) as db:
                                            add_ingredient(
                                                db=db,
                                                name=new_name,
                       
                                                quantity=parsed_qty,  # 分解した数値を入れる
                                                unit=parsed_unit,     # 分解した単位を入れる
                                                use_by_date=final_date
                                            )
                                            
                                            display_auto_clear_message(f"『{new_name}』を冷蔵庫に登録しました。", "success")
                                        
                                        st.session_state["registration_key"]= str(random.randint(0,100000))
                                        st.rerun()

                                except Exception as e:
                                    display_auto_clear_message(f"食材の登録中にエラーが発生しました: {e}", "error")
                                    
                            else:
                                display_auto_clear_message("食材名と数量は必須項目です。", "warning")
                                
                        # ----------------------------------------------------
                        # 4-2. 🆕 レシート画像からの食材登録エリア 📸
                        # ----------------------------------------------------
                        st.header("📸 レシートから食材を一括登録")
                        st.markdown("スーパーなどの**レシート画像**をアップロードすると、AIが自動で品目を抽出して食材リストに登録します。")

                        uploaded_receipt = st.file_uploader(
                            "レシート画像を選択してください",
                            type=["png", "jpg", "jpeg"],
                            key="receipt_uploader"
                        )

                        # 💡 修正点1: 抽出状態を保持するためのセッションステートキーを初期化
                        if "extracted_receipt_data" not in st.session_state:
                            st.session_state["extracted_receipt_data"] = None

                        if uploaded_receipt is not None:
                            st.image(uploaded_receipt, caption="アップロードされたレシート", use_container_width=True) # use_container_widthに修正
                            
                            # 抽出処理 (初回処理または再抽出)
                            if st.button("レシートから食材を抽出する 🚀", key="extract_receipt_button",type="primary"):
                                with st.spinner("AIがレシートを解析中です..."):
                                    extracted_items = process_receipt(uploaded_receipt)

                                if extracted_items:
                                    items_df = pd.DataFrame(extracted_items)
                                    # 💡 修正点A: チェックボックス列の追加。初期値は全てTrue（登録対象）とする
                                    items_df.insert(0, '登録対象', True) 
                                    items_df['期限'] = (datetime.date.today() + datetime.timedelta(days=7)).strftime('%Y-%m-%d')
                                    items_df.rename(columns={'name': '食材名', 'quantity': '数量'}, inplace=True)
                                    
                                    st.session_state["extracted_receipt_data"] = items_df.to_dict('records')
                                    display_auto_clear_message(f"✅ レシートから {len(extracted_items)} 個の品目を抽出しました。内容を確認してください。", "success")
                                    st.rerun()

                                else:
                                    display_auto_clear_message("レシートから品目を抽出できませんでした。画像が鮮明かご確認ください。", "warning")
                                    st.session_state["extracted_receipt_data"] = None

                        # 💡 修正点2: 抽出データがセッションステートにあれば、編集エリアと登録ボタンを表示する
                        if st.session_state["extracted_receipt_data"] is not None:
                            
                            st.subheader("抽出された品目（登録内容の確認・編集）")
                            
                            current_df = pd.DataFrame(st.session_state["extracted_receipt_data"])
                            
                            # 💡 修正点B: 登録対象列をチェックボックスとして表示するように設定
                            edited_df = st.data_editor(
                                current_df,
                                column_config={
                                    "登録対象": st.column_config.CheckboxColumn(
                                        "登録対象", # チェックボックスとして表示する列名
                                        default=True,
                                        help="チェックを外すと、この品目は登録されません。"
                                    )
                                },
                                num_rows="dynamic",
                                hide_index=True,
                                key="edited_receipt_items" 
                            )

                            # 💡 修正点3: 最終登録ボタンのロジック
                            if st.button("確認した品目を冷蔵庫に一括登録する ✨", key="final_receipt_register"):
                                successful_count = 0
                                
                                # 💡 修正点C: 登録対象の行のみをフィルタリングする
                                items_to_register = edited_df[edited_df['登録対象'] == True]
                                
                                try:
                                    with next(get_db()) as db:
                                        # 編集・フィルタリングされたデータフレームの各行を登録
                                        for index, row in items_to_register.iterrows():
                                            # 必須チェック
                                            if row['食材名'] and str(row['数量']).strip(): 
                                                use_by_date_obj = row['期限']
                                                
                                                # (省略: 日付オブジェクトの型変換ロジック - 変更なし)
                                                if isinstance(use_by_date_obj, pd.Timestamp):
                                                    use_by_date_obj = use_by_date_obj.date()
                                                elif isinstance(use_by_date_obj, str):
                                                    try:
                                                        use_by_date_obj = datetime.datetime.strptime(use_by_date_obj, "%Y-%m-%d").date()
                                                    except ValueError:
                                                        use_by_date_obj = datetime.date.today() + datetime.timedelta(days=7)
                                                elif not isinstance(use_by_date_obj, datetime.date):
                                                    use_by_date_obj = datetime.date.today() + datetime.timedelta(days=7)

                                                # 1. 「100g」などの文字を、数字(100)と単位(g)に切り分ける
                                                p_qty, p_unit = parse_quantity_and_unit(row['数量'])

                                                # 2. 切り分けたデータをデータベースに送る
                                                add_ingredient(
                                                    db=db,
                                                    name=row['食材名'],
                                                    quantity=p_qty if p_qty is not None else 1.0,  # 数字を入れる（空なら1.0）
                                                    unit=p_unit if p_unit else "個",               # 単位を入れる（空なら"個"）
                                                    use_by_date=use_by_date_obj,
                                                    commit=False 
                                                )
                                                successful_count += 1
                                                
                                        # すべての登録が完了した後で一度だけコミット
                                        db.commit() 
                                        
                                    display_auto_clear_message(f"🎉 {successful_count} 個の品目を冷蔵庫に登録しました！", "success")
                                    
                                    # UX改善: 登録成功後、セッションステートとアップロードをクリアして画面を初期状態に戻す
                                    st.session_state["extracted_receipt_data"] = None 
                                    
                                    st.rerun()

                                except Exception as e:
                                    # 登録失敗時、詳細なエラーメッセージを表示
                                    display_auto_clear_message(f"品目の一括登録中にエラーが発生しました: {e}", "error")
                                                    
                                else:
                                    st.warning("レシートから品目を抽出できませんでした。画像が鮮明かご確認ください。")
                                    
                        st.markdown("---") # 次のセクションと区切る
                    
                    # 食材リストの更新・変更タブ   
                    with tab_list_and_update_ingredients:
                            
                        # ----------------------------------------------------
                        # 🆕 DAY 1 & 2 目標: 食材リストの表示、編集、削除機能の実装
                        # ----------------------------------------------------
                        st.header("📋 冷蔵庫の食材リスト")

                        ingredients_df = pd.DataFrame() # 初期化

                        try:
                            with next(get_db()) as db:
                                ingredients = get_all_ingredients(db)
                                
                                if ingredients:
                                    data = [{
                                        "削除": False, 
                                        "id": item.id, 
                                        "食材名": item.name,
                                        # 💡 数字と単位をくっつけて表示する
                                        "数量": f"{item.quantity}{item.unit}" if item.unit else str(item.quantity),
                                        "期限": item.use_by_date,
                                        "unit": item.unit  # 内部処理用に単位だけも持っておく
                                    } for item in ingredients]
                                    
                                    ingredients_df = pd.DataFrame(data)

                                    # 警告ステータスの計算 (期限間近の強調表示の代替)
                                    def check_expiry_status(expiry_date):
                                        # datetime.date型に変換（PandasのTimestamp型の場合も考慮）
                                        if hasattr(expiry_date, 'date'):
                                            expiry_date = expiry_date.date()
                                        elif isinstance(expiry_date, str):
                                            # 文字列として保存されている可能性があるため、処理を追加
                                            try:
                                                expiry_date = datetime.datetime.strptime(expiry_date, '%Y-%m-%d').date()
                                            except:
                                                return "" # 変換エラーの場合は空欄

                                        days_left = (expiry_date - datetime.date.today()).days
                                        if days_left <= 3:
                                            return "🚨 3日以内"
                                        elif 3 < days_left <= 7:
                                            return "⚠️ 7日以内"
                                        return ""

                                    # 警告ステータス列を追加
                                    ingredients_df['期限ステータス'] = ingredients_df['期限'].apply(check_expiry_status)
                                    
                                    # --- 食材リストの表示と削除チェックボックス ---
                                    # 警告ステータス列と、元の期限列（'期限'）の両方を表示に含める
                                    display_df = ingredients_df.drop(columns=["id"]).rename(columns={'期限ステータス': '期限', '期限': '消費期限'})

                                    edited_df = st.data_editor(
                                        display_df,
                                        column_config={
                                            "削除": st.column_config.CheckboxColumn("🗑️ 削除", default=False),
                                            "食材名": st.column_config.TextColumn("食材名", disabled=True), 
                                            "数量": st.column_config.TextColumn("数量/単位", disabled=True),
                                            # 新しい「期限」列はステータスを表示
                                            "期限": st.column_config.TextColumn("期限", disabled=True),
                                            # 元の「消費期限」列は日付を表示（列名を分かりやすく変更）
                                            "消費期限": st.column_config.DateColumn("日付", format="YYYY/MM/DD", disabled=True) 
                                        },
                                        hide_index=True,
                                        use_container_width=True,
                                        num_rows="fixed",
                                        key="main_ingredient_editor"
                                    )
                                    

                                    # 削除 ID リストの計算
                                    # 編集後のデータフレームの「削除」列を、元のデータフレームにインデックスで結合
                                    ingredients_with_checks = ingredients_df.copy()
                                    ingredients_with_checks["is_checked"] = edited_df["削除"].values
                                    
                                    # チェックが入った行のIDを抽出
                                    ids_to_delete = ingredients_with_checks[ingredients_with_checks["is_checked"] == True]["id"].tolist()

                                    st.markdown("---")
                                    
                                    # 削除ボタン
                                    if st.button("🔴 選択した食材を削除", key="delete_button_final", disabled=not bool(ids_to_delete),type="primary"):
                                        if ids_to_delete:
                                            with next(get_db()) as db_del:
                                                for ing_id in ids_to_delete:
                                                    delete_ingredient(db_del, ing_id, commit=False) 
                                                
                                                db_del.commit() 
                                                # display_auto_clear_message(f"✅ {len(ids_to_delete)}件の食材を削除しました。", "success")
                                                st.rerun()

                                    # --- C. 個別更新フォームの追加 (変更なし) ---
                                    st.header("📝 食材の個別修正・更新")
                                    
                                    # 1. 更新対象の食材を選択するドロップダウン
                                    update_target_options = [f"{item['id']}: {item['食材名']}" for item in ingredients_df.to_dict('records')]

                                    selected_update_target = st.selectbox(
                                        "更新したい食材を選択してください",
                                        options=update_target_options,
                                        index=0 if update_target_options else None,
                                        key="update_target_select"
                                    )

                                    # 2. 選択された食材の現在の情報を取得しフォームを表示
                                    if selected_update_target:
                                        selected_id = int(selected_update_target.split(':')[0].strip())
                                        current_item = ingredients_df[ingredients_df['id'] == selected_id].iloc[0]
                                        
                                        with st.expander(f"『{current_item['食材名']}』の情報を編集", expanded=True):
                                            
                                            # フォームキーは main() の先頭で初期化済み ('update_key')
                                            form_key_with_id = st.session_state.get('update_key', 'ingredient_update_form') + str(selected_id)
                                            
                                            with st.form(key=form_key_with_id):
                                                
                                                # 【修正点1】: 食材名入力フィールドを削除し、現在の名前を読み取り専用で表示
                                                st.markdown(f"**食材名**: **{current_item['食材名']}**") 
                                                
                                                # 数量・単位
                                                up_quantity = st.text_input(
                                                    "数量・単位", 
                                                    value=current_item['数量'], 
                                                    key="up_quantity"
                                                )
                                                
                                                # 期限の初期値設定
                                                initial_date = current_item['期限'] if pd.notna(current_item['期限']) else datetime.date.today()
                                                if isinstance(initial_date, pd.Timestamp):
                                                    initial_date = initial_date.date()
                                                
                                                up_use_by_date = st.date_input(
                                                    "賞味期限/消費期限", 
                                                    value=initial_date,
                                                    key="up_date"
                                                )
                                                
                                                update_button = st.form_submit_button(label='この食材を更新する ✨',type="primary")
                                            
                                            if update_button:
                                                # 1. 入力された「500ml」などを数字と単位に切り分ける
                                                p_qty, p_unit = parse_quantity_and_unit(up_quantity)

                                                # 2. 数字が正しく抽出できた場合のみ更新を実行
                                                if p_qty is not None:
                                                    try:
                                                        with next(get_db()) as db_upd:
                                                            update_ingredient(
                                                                db_upd,
                                                                ingredient_id=selected_id,
                                                                quantity=p_qty,              # 数値(500.0)
                                                                unit=p_unit if p_unit else "個", # 単位(ml)
                                                                use_by_date=up_use_by_date
                                                            )
                                                            display_auto_clear_message(f"✅ 食材 ID:{selected_id} 『{current_item['食材名']}』の情報を更新しました。", "success")
                                                            
                                                            # フォームリセットとリラン
                                                            st.session_state["update_key"] = str(random.randint(0,100000))
                                                            st.rerun()
                                                    except Exception as e:
                                                        display_auto_clear_message(f"食材の更新中にエラーが発生しました: {e}", "error")
                                                else:
                                                    # 数字が含まれていない入力（例：「たくさん」など）の場合
                                                    st.error("数量は '500g' のように、数字から入力してください。")
                                            
                                            
                                else:
                                    st.info("現在、冷蔵庫に食材は登録されていません。")
                                    
                        except Exception as e:
                            st.error(f"データベース接続エラー：{e}")

                        st.markdown("---") # 次のセクションと区切る
                        
                    # アレルギーリストの成形
                    allergy_text = st.session_state.get("allergy_input", "")
                    allergies_to_exclude = []
                    lines = allergy_text.split("\n")

                    for item in lines:
                        stripped_item = item.strip()
                        if stripped_item:
                            allergies_to_exclude.append(stripped_item)
                    
                    # 5-3. 買い物リスト表示・削除エリア (サイドバー)
                    st.sidebar.header("🛒 買い物リスト")

                    # 買い物リストのデータ取得
                    try:
                        with next(get_db()) as db:
                            shopping_items = get_all_shopping_items(db)

                            if shopping_items:
                                # 買い物リストをDataFrameに変換
                                shopping_df = pd.DataFrame([
                                    {"削除": False, "id": item.id, "品目": item.name, "提案レシピ": item.recipe_name}
                                    for item in shopping_items
                                ])

                                # 買い物リストの表示と編集 (st.data_editor)
                                edited_shopping_df = st.sidebar.data_editor( # 👈 st.sidebar を使用
                                    shopping_df,
                                    column_config={
                                        "削除": st.column_config.CheckboxColumn("済", default=False),
                                        "id": None, 
                                        "提案レシピ": st.column_config.TextColumn("提案レシピ", disabled=True),
                                        "品目": st.column_config.TextColumn("品目", disabled=True)
                                    },
                                    hide_index=True,
                                    num_rows="fixed",
                                    use_container_width=True,
                                    key="shopping_editor"
                                )

                                # 削除（完了）ボタン
                                st.sidebar.markdown("---")
                                col_done, col_clear = st.sidebar.columns(2)
                                
                                if col_done.button("✅ 選択した項目を完了", key="delete_shopping"):
                                    ids_to_delete = edited_shopping_df[edited_shopping_df["削除"] == True]["id"].tolist()
                                    
                                    if ids_to_delete:
                                        with next(get_db()) as db_del:
                                            for item_id in ids_to_delete:
                                                delete_shopping_item(db_del, item_id, commit=False)
                                            db_del.commit()
                                            st.sidebar.success(f"✅ {len(ids_to_delete)}件の買い物をリストから削除しました。", icon="🛒") # 👈 st.sidebar.success に変更
                                            st.rerun()
                                    else:
                                        st.sidebar.warning("完了する項目にチェックを入れてください。")
                                
                                # リスト全削除ボタン
                                if col_clear.button("🗑️ 全てクリア", key="clear_shopping"):
                                    with next(get_db()) as db_clear:
                                        clear_shopping_list(db_clear)
                                        st.sidebar.success("🗑️ 買い物リストを全てクリアしました。", icon="🛒") # 👈 st.sidebar.success に変更
                                        st.rerun()

                            else:
                                st.sidebar.info("買い物リストは空です。")
                                
                    except Exception as e:
                        st.sidebar.error(f"買い物リストDBエラー: {e}")
                            
                    # 5-4. レシピ履歴エリア (サイドバー)
                    st.sidebar.header("📜 レシピ履歴")

                    try:
                        with next(get_db()) as db_hist:
                            history_items = get_all_recipe_history(db_hist)
                            
                            if history_items:
                                # 履歴をドロップダウンメニューで表示
                                history_names = [f"ID:{item.id} - {item.recipe_name} ({item.created_at.strftime('%m/%d %H:%M')})" for item in history_items]
                                
                                selected_history = st.sidebar.selectbox(
                                    "閲覧するレシピを選択",
                                    options=history_names,
                                    index=0,
                                    key="selected_recipe_history"
                                )
                                
                                # 選択されたレシピのIDを抽出
                                selected_id = int(selected_history.split(" - ")[0].replace("ID:", ""))
                                
                                # 該当レシピの全文を取得
                                selected_recipe = next((item for item in history_items if item.id == selected_id), None)
                                
                                if selected_recipe:
                                    # メイン画面に履歴レシピを表示するためのボタン
                                    if st.sidebar.button("このレシピをメインに表示", key="show_history"):
                                        st.session_state["last_suggestion"] = selected_recipe.full_suggestion
                                        st.session_state["proposal_warning"] = False
                                        st.rerun()

                                    # 履歴削除機能
                                    if st.sidebar.button("この履歴を削除 ✖", key="delete_history_item"):
                                        success_delete = False
                                        try:
                                                with next(get_db()) as db_del_hist:
                                                    # commit=True (デフォルト)でcommitされる
                                                    if delete_recipe_history(db_del_hist, selected_id):
                                                        success_delete = True
                                                        display_auto_clear_message(f"✅ ID: {selected_id} のレシピ履歴を削除しました。", "success")
                                                    else:
                                                        st.sidebar.warning(f"ID: {selected_id} は見つかりませんでした。")
                                                
                                                if success_delete:
                                                    # 【修正２：削除後のクリア処理】提案表示をクリアしてリラン
                                                    st.session_state["last_suggestion"] = None 
                                                    st.rerun()
                                        
                                        except Exception as e:
                                            st.sidebar.error(f"レシピ履歴の削除中にエラーが発生しました: {e}")
                                        
                            else:
                                st.sidebar.info("レシピ履歴はありません。")
                                
                    except Exception as e:
                        st.sidebar.error(f"レシピ履歴表示エラー: {e}")

                    #献立提案タブ
                    with tab_menu:

                        # 5-5. 献立提案のための食材選択エリア
                        st.header("🛒 提案に使用する食材の選択")

                        if ingredients:
                            ingredient_names = [item.name for item in ingredients]

                            selected_ingredients_names = st.multiselect(
                                "提案に使いたい食材を選んでください (複数選択可)",
                                options=ingredient_names,
                                default=ingredient_names,
                                key="selected_ingredients_names_multiselect"
                                    )
                        else:
                            st.warning("提案する食材が登録されていません。")
                            selected_ingredients_names = []
                            
                        # 5-6. 調理時間選択エリア
                        st.header("⏳ 調理時間の設定")

                        cooking_time_option = st.radio(
                            "希望する調理時間帯を選択してください。",
                            options=["指定なし", "時短（10分以内）", "通常（30分以内）", "本格（30分以上）"],
                            index=0,
                            key="cooking_time_selection"
                        )

                        time_constraint_text = ""
                        if cooking_time_option == "時短（10分以内）":
                            time_constraint_text = "調理時間は**10分以内**で完了することを最優先してください。"
                        elif cooking_time_option == "通常（30分以内）":
                            time_constraint_text = "調理時間は**30分以内**で完了するレシピを提案してください。"
                        elif cooking_time_option == "本格（30分以上）":
                            time_constraint_text = "調理時間は**30分以上**かかる、手間をかけた本格的なレシピを提案してください。"

                            
                            
                        # 6. 献立提案エリア
                        st.header("🍳 今日の献立提案")
                        st.markdown("食材リストの選択状態に基づき、献立を提案します。")


                        if st.button("献立を提案してもらう！🤖", key="propose_menu_button",type="primary"):
                            
                            # 1. 提案に使用する食材リストの決定
                            if not selected_ingredients_names:
                                base_ingredients = ingredients
                            else:
                                base_ingredients = [
                                    item for item in ingredients 
                                    if item.name in selected_ingredients_names
                                ]

                            # アレルギー食材を強制的に除外
                            ingredients_for_proposal = [
                                item for item in base_ingredients
                                if item.name not in allergies_to_exclude
                            ]

                            # 2. 提案ロジックの実行
                            if ingredients_for_proposal:
                                with st.spinner("献立を考えています...🤖"):
                                    suggestion = suggest_menu(
                                        ingredients_for_proposal, 
                                        allergies_to_exclude, 
                                        time_constraint_text
                                    )
                                
                                # レシピ履歴への保存ロジック
                                try:
                                    # 提案されたレシピ名を取得
                                    recipe_line = suggestion.split("1. **提案名**：")
                                    recipe_name = "提案レシピ"
                                    if len(recipe_line) > 1:
                                        # **レシピ名**のような形式を考慮し、最も信頼性の高い形式にパース
                                        recipe_name_raw = recipe_line[1].split('\n')[0].strip()
                                        if recipe_name_raw.startswith("**") and recipe_name_raw.endswith("**"):
                                            recipe_name = recipe_name_raw.strip('**')
                                        else:
                                            recipe_name = recipe_name_raw

                                    # DBにレシピ全体とレシピ名を履歴として保存
                                    with next(get_db()) as db_hist:
                                        # commit=True (デフォルト)でcommitされる
                                        add_recipe_history(
                                            db_hist, 
                                            recipe_name=recipe_name, 
                                            full_suggestion=suggestion
                                        )
                                except Exception as e:
                                    st.warning(f"レシピ履歴の保存中にエラーが発生しました: {e}") 
                                # ------------------------------------
                                
                                # 既存のセッションステート保存
                                st.session_state["last_suggestion"] = suggestion
                                st.session_state["proposal_warning"] = (
                                    len(base_ingredients) != len(ingredients_for_proposal)
                                )
                                
                                # 提案を保存したら、画面をリランして下の表示ロジックへ移行させる
                                st.rerun() 
                                
                            else:
                                st.warning("献立提案に必要な食材が冷蔵庫にありません。食材を登録するか、アレルギー設定を確認してください。")

                        # ----------------------------------------------------
                        # 提案結果の表示ロジック
                        # ----------------------------------------------------
                        
                        suggestion = st.session_state["last_suggestion"]

                        if suggestion: # セッションステートに提案結果が残っていれば表示する
                            if st.session_state.get("proposal_warning"):
                                st.warning("⚠️ 選択された食材の一部にアレルギー・除外食材が含まれていたため、提案から除外されました。")
                            
                            st.info(suggestion)
                            
                            # ------------------------------------
                            # 自動減算処理ロジック
                            # ------------------------------------
                            if st.button("この献立で確定（在庫を減らす）", key="confirm_menu_button", type="primary"):
                                # 1. 提案テキストから使用食材を抜き出す
                                used_items = parse_ingredients_from_recipe(suggestion)
                                
                                if used_items:
                                    with next(get_db()) as db:
                                        for item in used_items:
                                            # 2. データベースの在庫を減らす (数量のみを引数に渡す)
                                            success, message = update_ingredient_quantity(
                                                db, item["name"], item["quantity"]
                                            )
                                            if success:
                                                st.success(message)
                                            else:
                                                st.warning(message)
                                    # 3. 在庫を減らした後に画面を更新
                                    st.rerun()
                                else:
                                    st.error("レシピから食材の分量を読み取れませんでした。形式を確認してください。")
                            
                            
                            
                            # 3. 買い物リストへの追加ロジック 
                            shopping_list_marker_candidates = ["不足食材・買い物リスト", "5. 不足食材・買い物リスト"]
                            shopping_list_start_index = -1

                            for marker in shopping_list_marker_candidates:
                                if marker in suggestion:
                                    shopping_list_start_index = suggestion.find(marker)
                                    break

                            shopping_list_raw = ""
                            if shopping_list_start_index != -1:
                                start_of_list = suggestion.find('\n', shopping_list_start_index)
                                if start_of_list != -1:
                                    end_marker_candidates = ["提案理由", "調理手順", "6. 提案理由", "7. 調理手順"]
                                    end_index = len(suggestion)

                                    for end_marker in end_marker_candidates:
                                        current_end_index = suggestion.find(end_marker, start_of_list)
                                        if current_end_index != -1 and current_end_index < end_index:
                                            end_index = current_end_index
                                    
                                    shopping_list_raw = suggestion[start_of_list:end_index].strip()
                                    
                                    extracted_items = []
                                    for line in shopping_list_raw.split('\n'):
                                        # 💡 1. まず余計な記号を取り除く
                                        clean_item = line.strip().lstrip('*- ').strip()
                                        
                                        # 💡 2. 以下の条件に当てはまるものは「食材ではない」と判断してスキップする
                                        # - 文字が空っぽ
                                        # - 「なし」や「特になし」
                                        # - 「。」（句点）が含まれる（＝食材名ではなく説明文である可能性が高い）
                                        # - 「6.」などの見出し数字で始まる
                                        if not clean_item:
                                            continue
                                        if clean_item in ["なし", "特になし"]:
                                            continue
                                        if "。" in clean_item: # 句点があれば説明文とみなす
                                            continue
                                        if clean_item.startswith(("6.", "7.", "提案理由")): # 見出しを直接除外
                                            continue
                                            
                                        extracted_items.append(clean_item)
                                    
                                    if extracted_items:
                                        with st.expander("🛒 抽出された買い物リストを確認する", expanded=True):
                                            st.write("以下の食材を買い物リストに登録しますか？")
                                            st.text_area("抽出された食材リスト", "\n".join(extracted_items), height=100, disabled=True)
                                            
                                            if st.button("このリストを買い物リストに登録する 🛒✨", key="save_shopping_list",type="primary"):
                                                try:
                                                    with next(get_db()) as db_shop:
                                                        recipe_line = suggestion.split("1. **提案名**：")
                                                        recipe_name = "提案レシピ"
                                                        if len(recipe_line) > 1:
                                                            recipe_name_raw = recipe_line[1].split('\n')[0].strip()
                                                            if recipe_name_raw.startswith("**") and recipe_name_raw.endswith("**"):
                                                                recipe_name = recipe_name_raw.strip('**')
                                                            else:
                                                                recipe_name = recipe_name_raw

                                                        for item_name in extracted_items:
                                                            # 【修正１：commit=Falseで一括追加】複数回のコミットを防ぐ
                                                            add_shopping_item(db_shop, name=item_name, recipe_name=recipe_name, commit=False)
                                                            
                                                        # ループが終わった後に一度だけコミットを実行
                                                        db_shop.commit() 
                                                        
                                                        display_auto_clear_message("✅ 買い物リストに登録しました！サイドバーを確認してください。", "success")
                                                    
                                                        st.rerun() 

                                                except Exception as e:
                                                    st.error(f"買い物リストの登録中にエラーが発生しました: {e}")
                                                    
                                    else:
                                        st.success("🎉 献立に必要な追加の食材・調味料は特にありませんでした！")

                        # ----------------------------------------------------
                        # 7. 会話形式の調整エリア (順序6)
                        # ----------------------------------------------------
                        st.header("💬 レシピの調整・相談")

                        if suggestion: # レシピが提案されている場合のみ表示
                            # 調整フォーム
                            with st.form(key=st.session_state["adjustment_form_key"]):
                                adjustment_prompt = st.text_input(
                                    "レシピに対する要望を入力してください。",
                                    placeholder="例: 調理時間を15分以内に短縮して、または、肉を魚に変えて",
                                    key="adjustment_input"
                                )
                                adjust_button = st.form_submit_button("レシピを調整・修正する 🔄",type="primary")

                            # 調整ロジック
                            if adjust_button:
                                if adjustment_prompt:
                                    
                                    # 1. 現在のレシピ（文脈）を取得
                                    current_recipe = st.session_state["last_suggestion"]
                                    
                                    # 2. 調整用プロンプトの作成
                                    adjustment_system_prompt = "あなたは優秀な料理専門家です。ユーザーの要望に基づき、以下のレシピを修正・再提案してください。形式は元のレシピの形式を厳守してください。特に、要望された調理時間を厳密に守るため、調理法の変更（例：オーブンから炒め物へ）を躊躇しないでください。"

                                    # ユーザープロンプトを最終的に強制力最大化 (ロジックの自己矛盾を強制)
                                    # 【プロンプト改善３】短縮調理法の制約を強化し、Markdownを削減
                                    adjustment_user_prompt = f"""
                                    【元のレシピ】
                                    {current_recipe}

                                    【ユーザーの要望（この要望に従ってレシピを修正してください）】
                                    {adjustment_prompt}
                                    
                                    ---
                                    
                                    🛑 **最重要！絶対厳守ルール：要望時間の厳密な遵守** 🛑
                                    
                                    1. **合計時間の厳密遵守:** レシピ冒頭の「調理時間：XX分」は、**食材カット、予熱、加熱、盛り付けの全工程の合計時間**を指します。要望された時間（例: 15分）を**1分たりとも超えてはいけません**。
                                    2. **調理法変更の強制:** 元のレシピの調理法（例：オーブン焼き）を維持すると**合計調理時間が要望時間（例：15分）をオーバーする場合**、その調理法は**採用不可能です**。**即座に短時間で完了する代替調理法（例：炒め物、レンジ調理）に切り替えてください。**
                                    3. **内訳の提示強制:** レシピの「調理時間」の欄には、必ず**「調理時間：Z分（内訳：下準備X分、加熱Y分）」**のように内訳を追記し、合計時間が要望時間を超えないことを数学的に証明してください。

                                    ## 修正点
                                    
                                    - レシピの最下部に「修正点」セクションを設け、元のレシピから具体的に何が変わったか（特に調理法、時間内訳、工程）を箇条書きで明確に説明してください。
                                    
                                    修正した後の新しいレシピ全文を、元のレシピの形式を完全に守って出力してください。
                                    """
                                    
                                    with st.spinner("レシピを調整中です... 🧠"):
                                        try:
                                            # OpenAI APIの呼び出し
                                            client = OpenAI(api_key=st.secrets["openai"]["api_key"]) 
                                            
                                            response = client.chat.completions.create(
                                                model="gpt-4o",
                                                messages=[
                                                    {"role": "system", "content": adjustment_system_prompt},
                                                    {"role": "user", "content": adjustment_user_prompt}
                                                ],
                                                temperature=0.8
                                            )
                                            
                                            new_suggestion = response.choices[0].message.content
                                            
                                            # 3. 調整後のレシピをセッションステートに上書きし、画面更新
                                            st.session_state["last_suggestion"] = new_suggestion
                                            
                                            st.session_state["adjustment_form_key"] = str(random.randint(0,100000)) 
                                            
                                            st.rerun()
                                            
                                        except Exception as e:
                                            st.error(f"🚨 レシピ調整APIの呼び出し中にエラーが発生しました。\n原因: {e}")
                                            
                                else:
                                    st.warning("調整内容を入力してください。")
                        else:
                            # 提案結果がない場合のメッセージ
                            st.info("まず「献立を提案してもらう」ボタンでレシピを提案させてください。")
                        
                        pass
            
                main()
                
elif authentication_status is False:
    # ログイン失敗時
    st.error('ユーザー名またはパスワードが正しくありません')
    
elif authentication_status is None:
    # 未ログイン時
    st.warning('ユーザー名とパスワードを入力してください')