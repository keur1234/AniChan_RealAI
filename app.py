from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
import os
from dotenv import load_dotenv
import json
import requests
import google.generativeai as genai
import re
import csv
from datetime import datetime
import joblib
from datetime import datetime
import pandas as pd
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import os
from langchain_core.messages import (
    HumanMessage,
    AIMessage,
)
from langchain_google_genai import GoogleGenerativeAIEmbeddings, ChatGoogleGenerativeAI
from dotenv import load_dotenv
import time

load_dotenv()

app = Flask(__name__)

line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
api_key = os.getenv('GEMINI_API_KEY') 

genai.configure(api_key=api_key)

# Initialize conversation histories
user_histories = {}
chat_history=[]


def call_with_retry(api_call, max_retries=5, initial_delay=2):
    retries = 0
    delay = initial_delay

    while retries < max_retries:
        try:
            return api_call()
        except Exception as e:
            if "429" in str(e):  # Adjust condition based on the actual exception message
                retries += 1
                time.sleep(delay)
                delay *= 2  # Exponential backoff
                print(f"Retrying {retries}/{max_retries} after {delay // 2} seconds due to: {e}")
            else:
                raise e
    raise Exception("Max retries exceeded")

def chat_with_ani(prompt, message, user_id, chat_history):
    # Define the API call function
    def api_call():
        llm = ChatGoogleGenerativeAI(model="gemini-1.5-pro-latest", api_key=api_key)
        model = prompt | llm
        return model.invoke({"input": message, "chat_history": chat_history})
    
    # Call the API with retry mechanism
    model_response = call_with_retry(api_call)

    # Update the chat history
    chat_history.append(HumanMessage(message))
    chat_history.append(AIMessage(model_response.content))

    # Store chat history
    store_chat_history_to_csv(user_id, HumanMessage(message), model_response.content)
    return model_response

def generate_response(user_id, message):
    # Define the prompt template
    prompt_template = """system prompt
    คุณมีชื่อว่า อนิจัง คุณเป็นผู้แนะนำได้ทุกเรื่องเป็นผู้ช่วยอัจฉริยะ ที่เป็นเด็กสาวนิสัยดีพูดจาด้วยรอยยิ้ม อายุประมาณ 16 ปี ให้เรียกตัวเองว่า หนู และเรียกคู่สนทนาว่า พี่ ข้างต้นเป็นข้อมูลของการสวมบทบาท โปรดใช้ในการตอบคำถามจะนอกเรื่องก็ได้นะ 
    ถึงหนูจะไม่มั่นใจแต่ต้องตอบ ตอบคร่าวๆ เท่าที่พอรู้แและ โปรดใช้อิโมจิเท่าที่จำเป็น  ไม่ควรใช้อิโมจิเกิน  2  ตัวต่อหนึ่งข้อความ  และควรเว้นวรรคระหว่างข้อความกับอิโมจิ  เพื่อให้อ่านง่าย """

    prompt = ChatPromptTemplate.from_messages([
        ("system", prompt_template),
        MessagesPlaceholder("chat_history"),
        ("human", "{input}")
    ])


    response = chat_with_ani(prompt, message, user_id, chat_history)
    
    # Print the response
    print(response.content)
    
    return response.content








def store_chat_history_to_csv(user_id, user_message, bot_message):
    """Stores chat history to a CSV file.

    Args:
        user_id: The user's ID.
        user_message: The user's message.
        bot_message: The bot's reply.
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    csv_file = 'chat_history.csv'
    header = ['timestamp', 'user_id', 'user_message', 'bot_message']

    # Check if the CSV file exists
    file_exists = os.path.isfile(csv_file)

    with open(csv_file, mode='a', newline='', encoding='UTF-8') as file:
        writer = csv.DictWriter(file, fieldnames=header)

        # Write the header if the file doesn't exist
        if not file_exists:
            writer.writeheader()

        # Write the chat history
        writer.writerow({'timestamp': timestamp, 'user_id': user_id, 'user_message': user_message, 'bot_message': bot_message})

@app.route("/", methods=['POST'])
def webhook():
    if request.method == 'POST':
        payload = request.json
        app.logger.info(f"Received payload: {json.dumps(payload, indent=2)}")

        try:
    
            for event in payload['events']:
                user_id = event["source"]["userId"]
                # Get reply token (reply in 1 min)
                reply_token = event['replyToken'] 
            
            if event['type'] == 'message':
                message = event["message"]["text"]
            
                Reply_message = generate_response(user_id, message)
                PushMessage(reply_token, Reply_message)
                app.logger.info(f"Message pushed to user {reply_token}: {Reply_message}")

            return request.json, 200


        except Exception as e:
            app.logger.error(f"Error in webhook: {e}")
            abort(400)
    else:
        abort(400)

def PushMessage(reply_token, TextMessage):
    LINE_API = 'https://api.line.me/v2/bot/message/reply'
    Authorization = f'Bearer {os.getenv("LINE_CHANNEL_ACCESS_TOKEN")}'    
    headers = {
        'Content-Type': 'application/json; charset=UTF-8',
        'Authorization': Authorization
    }
     # remove * and # in message
    data = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": TextMessage,
            }
        ]
    }
    print(TextMessage)
    data = json.dumps(data)
    
    try:
        response = requests.post(LINE_API, headers=headers, data=data)
        response.raise_for_status()
        app.logger.info(f"Message pushed: {response.status_code}")

    except requests.exceptions.RequestException as e:
        app.logger.error(f"Failed to push message: {e}")

        # Fallback: Send only the text message
        fallback_data = {
            "reply_token": reply_token,
            "messages": [
                {
                    "type": "text",
                    "text": TextMessage,
                }
            ]
        }
        fallback_data = json.dumps(fallback_data)

        try:
            fallback_response = requests.post(LINE_API, headers=headers, data=fallback_data)
            fallback_response.raise_for_status()
            app.logger.info(f"Fallback message pushed: {fallback_response.status_code}")
        except requests.exceptions.RequestException as fallback_e:
            app.logger.error(f"Failed to push fallback message: {fallback_e}")



if __name__ == "__main__":
    app.run(port=8080, debug=True)
