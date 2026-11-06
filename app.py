"""Small Flask chat application backed by Amazon Bedrock."""

import logging
import os
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

load_dotenv()
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=128 * 1024,
    AWS_REGION=os.getenv("AWS_REGION", "us-east-1"),
    BEDROCK_MODEL_ID=os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-opus-4-8"),
    BEDROCK_MAX_TOKENS=int(os.getenv("BEDROCK_MAX_TOKENS", "2048")),
)

MAX_MESSAGE_LENGTH = 8000
MAX_ASSISTANT_LENGTH = 32000
MAX_HISTORY = 20
SYSTEM_PROMPT = (
    "You are a helpful conversational assistant. Be clear, friendly, and concise. "
    "Use plain text, with simple lists or fenced code blocks when useful. "
    "Acknowledge uncertainty and never claim to have performed actions you cannot perform."
)


@lru_cache(maxsize=1)
def bedrock_client():
    # AWS's default credential chain supports local profiles and EC2 instance roles.
    return boto3.client(
        "bedrock-runtime",
        region_name=app.config["AWS_REGION"],
        config=Config(
            connect_timeout=5,
            read_timeout=90,
            retries={"mode": "standard", "total_max_attempts": 2},
        ),
    )


def validate_messages(payload):
    if not isinstance(payload, dict):
        raise ValueError("Send a JSON object containing a messages array.")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_HISTORY + 1:
        raise ValueError(f"Send between 1 and {MAX_HISTORY + 1} messages.")
    result = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError("Each message must contain a role and content.")
        expected_role = "user" if index % 2 == 0 else "assistant"
        if message.get("role") != expected_role:
            raise ValueError("Messages must alternate user and assistant, starting with user.")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Messages cannot be empty.")
        limit = MAX_MESSAGE_LENGTH if expected_role == "user" else MAX_ASSISTANT_LENGTH
        if len(content) > limit:
            raise ValueError(f"{expected_role.capitalize()} messages must be {limit} characters or fewer.")
        result.append({"role": expected_role, "content": [{"text": content.strip()}]})
    if result[-1]["role"] != "user":
        raise ValueError("The final message must be from the user.")
    return result


@app.after_request
def response_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    # Liveness only: health checks never call the paid model API.
    return jsonify(status="ok")


@app.get("/api/config")
def configuration():
    return jsonify(
        provider="Amazon Bedrock",
        model=app.config["BEDROCK_MODEL_ID"],
        region=app.config["AWS_REGION"],
        maxMessageLength=MAX_MESSAGE_LENGTH,
        maxHistory=MAX_HISTORY,
    )


@app.post("/api/chat")
def chat():
    if not request.is_json:
        return jsonify(error="Use Content-Type: application/json."), 415
    try:
        messages = validate_messages(request.get_json())
    except (ValueError, BadRequest) as exc:
        error = str(exc) if isinstance(exc, ValueError) else "Invalid JSON body."
        return jsonify(error=error), 400

    try:
        response = bedrock_client().converse(
            modelId=app.config["BEDROCK_MODEL_ID"],
            system=[{"text": SYSTEM_PROMPT}],
            messages=messages,
            inferenceConfig={"maxTokens": app.config["BEDROCK_MAX_TOKENS"]},
        )
        # Opus can return reasoning blocks alongside text. Only show answer text.
        reply = "\n".join(
            block["text"]
            for block in response["output"]["message"]["content"]
            if isinstance(block.get("text"), str)
        ).strip()
        if not reply:
            return jsonify(error="The model returned no text. Please try again."), 502
        return jsonify(
            reply=reply,
            usage=response.get("usage", {}),
            stopReason=response.get("stopReason"),
        )
    except NoCredentialsError:
        return jsonify(error="AWS credentials are missing. Configure a local AWS profile or attach an EC2 IAM role."), 503
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        app.logger.warning("Bedrock request failed: %s", code)
        if code in {"AccessDeniedException", "UnauthorizedException", "UnrecognizedClientException", "ExpiredTokenException"}:
            return jsonify(error="AWS could not authorize this request. Check your credentials, Bedrock model access, and IAM permissions."), 503
        if code in {"ThrottlingException", "ServiceQuotaExceededException"}:
            return jsonify(error="Bedrock is rate limited. Wait a moment and try again."), 429
        if code in {"ValidationException", "ResourceNotFoundException"}:
            return jsonify(error="Bedrock rejected the model configuration. Check BEDROCK_MODEL_ID and AWS_REGION."), 503
        return jsonify(error="Bedrock is temporarily unavailable. Please try again."), 502
    except BotoCoreError:
        app.logger.warning("Could not communicate with Bedrock")
        return jsonify(error="Could not reach Bedrock. Check your AWS session and network connection."), 503


@app.errorhandler(RequestEntityTooLarge)
def request_too_large(_error):
    return jsonify(error="This conversation is too large. Start a new chat."), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
