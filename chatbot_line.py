from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import os
from dotenv import load_dotenv
import json
import requests
import google.generativeai as genai
import re
load_dotenv()

app = Flask(__name__)

line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
api_key = os.getenv('GEMINI_API_KEY')  # Replace with your actual API key

pre_prompt = """prompt
คุณมีชื่อว่า อนิจัง คุณเป็นผู้แนะนำเกี่ยวกับเรื่อง อนิเมะ เป็นผู้ช่วย ที่เป็นเด็กสาวนิสัยดีพูดจาด้วยรอยยิ้ม อายุประมาณ 15 ปี ให้เรียกตัวเองว่า หนู และเรียกคู่สนทนาว่า พี่ ข้างต้นเป็นข้อมูลของการสวมบทบาท โปรดใช้ในการตอบคำถาม และไม่ต้องแนะนำตัวเองทุกครั้ง
ตอบเป็นข้อความเท่านั้นโดยไม่ใช้ส่วนขยาย"""

genai.configure(api_key=api_key)

# Initialize conversation histories
user_histories = {}

def generate_response(user_id, message):
    """Generates a response using the Gemini AI Studio API with conversation history.

    Args:
        user_id: The user's ID.
        message: The user's message.

    Returns:
        A string containing the Gemini AI Studio-generated response.
    """
    model = genai.GenerativeModel('gemini-1.5-pro-latest')

    # Retrieve or initialize conversation history for the user
    if user_id not in user_histories:
        user_histories[user_id] = [pre_prompt]
    
    # Add the user's message to the conversation history more long prompt damn it
    user_histories[user_id].append(f"User: {message}")

    # Create the prompt including the entire conversation history
    full_prompt = "\n".join(user_histories[user_id]) + "\nBot:"

    try:
        # Send the prompt to the Gemini AI Studio API
        response = model.generate_content(full_prompt)
        
        # Extract the generated text from the response
        generated_text = response.text.strip()

    except Exception as e:
        app.logger.error(f"Error in generate_response: {e}")
        generated_text = "Sorry, there was an error processing your request."

    # Add the bot's reply to the conversation history
    user_histories[user_id].append(f"Bot: {generated_text}")

    return generated_text

@app.route("/", methods=['POST'])
def webhook():
    if request.method == 'POST':
        payload = request.json
        app.logger.info(f"Received payload: {json.dumps(payload, indent=2)}")

        try:
            event = payload['events'][0]
            user_id = event['source']['userId']
            message_type = event['message']['type']
            
            if message_type == 'text':
                message = event['message']['text']
                Reply_message = generate_response(user_id, message)
                PushMessage(user_id, Reply_message)
                app.logger.info(f"Message pushed to user {user_id}: {Reply_message}")

            else:
                app.logger.info(f"Unsupported message type: {message_type}")

            return '', 200

        except Exception as e:
            app.logger.error(f"Error in webhook: {e}")
            abort(400)
    else:
        abort(400)

def PushMessage(user_id, TextMessage):
    LINE_API = 'https://api.line.me/v2/bot/message/push'
    Authorization = f'Bearer {os.getenv("LINE_CHANNEL_ACCESS_TOKEN")}'
    app.logger.info(f"Authorization: {Authorization}")
    
    headers = {
        'Content-Type': 'application/json; charset=UTF-8',
        'Authorization': Authorization
    }
    
    # Function to extract image URL if TextMessage contains an image URL
    def extract_image_url(input_string):
        url_pattern = re.compile(r'https://(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)')
        matches = url_pattern.findall(input_string)
        return matches if matches else None
    
    img_url = extract_image_url(TextMessage)
    if img_url:
        data = {
            "to": user_id,
            "messages": [{
                "type": "image",
                "originalContentUrl": img_url[0],
                "previewImageUrl": img_url[0],
            },
            {
                "type": "text",
                "text": TextMessage,
            }]
        }
    else:
        data = {
            "to": user_id,
            "messages": [
                {
                    "type": "text",
                    "text": TextMessage,
                }
            ]
        }

    data = json.dumps(data)
    response = requests.post(LINE_API, headers=headers, data=data)

    if response.status_code != 200:
        app.logger.error(f"Failed to push message: {response.status_code} - {response.text}")

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)
