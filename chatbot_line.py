from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import gemini_api
import os
from dotenv import load_dotenv
import json
import requests
import google.generativeai as genai

load_dotenv()

app = Flask(__name__)

line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
api_key = os.getenv('GEMINI_API_KEY')  # Replace with your actual API key
genai.configure(api_key=api_key)

def generate_response(prompt):
    """Generates a response using the Gemini AI Studio API.

    Args:
        prompt: The user's query or message.

    Returns:
        A string containing the Gemini AI Studio-generated response.
    """
    model = genai.GenerativeModel('gemini-1.5-pro-latest')

    # Send the prompt to the Gemini AI Studio API
    response = model.generate_content(prompt)

    # # Extract the generated text from the response
    # generated_text = response["choices"][0]["text"].strip()

    return response.text.strip()


@app.route("/", methods=['POST'])
def webhook():
    if request.method == 'POST':
        payload = request.json
        # Log the entire payload for debugging
        app.logger.info(f"Received payload: {json.dumps(payload, indent=2)}")
        
        
        try:
            user_id = payload['events'][0]['source']['userId']
            message = payload['events'][0]['message']['text']

            # Assuming model.QA is a function to generate a response based on the message and user_id
            # Replace this with your actual model logic
            pre_prompt = "You are a friendly chatbot that helps users with their queries."
            full_prompt = f"{pre_prompt}\n\nUser: {message}\nBot:"
            Reply_message = generate_response(full_prompt)
            
            PushMessage(user_id, Reply_message)
            print("Pushing message")
            return request.json, 200
        
        except Exception as e:
            app.logger.error(f"Error: {e}")
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
    def extract_image_url(text):
        # Implement your logic to extract image URL from text
        # This is a placeholder function
        return None
    
    # Check if TextMessage contains an image URL
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
    # Convert the dictionary to a JSON string
    data = json.dumps(data)
    
    # Send the POST request to the LINE API
    requests.post(LINE_API, headers=headers, data=data)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)
