#!/bin/bash
# Test script for slm_server
# Tests the slm_server endpoints before testing the agent

set -e

SLM_SERVER_URL="http://localhost:8000"

echo "🧪 Testing SLM Server"
echo "===================="
echo ""

# Test 1: Health check
echo "Test 1: Health check"
echo "GET $SLM_SERVER_URL/health"
response=$(curl -s -w "\nHTTP_CODE:%{http_code}" "$SLM_SERVER_URL/health" || echo "HTTP_CODE:000")
http_code=$(echo "$response" | grep "HTTP_CODE" | cut -d: -f2)
body=$(echo "$response" | sed '/HTTP_CODE/d')

if [ "$http_code" = "200" ]; then
    echo "✅ Health check passed"
    echo "Response: $body"
else
    echo "❌ Health check failed (HTTP $http_code)"
    echo "Response: $body"
    echo ""
    echo "Is slm_server running?"
    echo "  Check: curl http://localhost:8000/health"
    exit 1
fi
echo ""

# Test 2: List models
echo "Test 2: List available models"
echo "GET $SLM_SERVER_URL/v1/models"
response=$(curl -s -w "\nHTTP_CODE:%{http_code}" "$SLM_SERVER_URL/v1/models" || echo "HTTP_CODE:000")
http_code=$(echo "$response" | grep "HTTP_CODE" | cut -d: -f2)
body=$(echo "$response" | sed '/HTTP_CODE/d')

if [ "$http_code" = "200" ]; then
    echo "✅ List models passed"
    echo "$body" | python3 -m json.tool 2>/dev/null || echo "$body"
else
    echo "❌ List models failed (HTTP $http_code)"
    echo "Response: $body"
    exit 1
fi
echo ""

# Test 3: Chat completions (simple request)
echo "Test 3: Chat completions request"
echo "POST $SLM_SERVER_URL/v1/chat/completions"
request_body='{
  "model": "qwen/qwen3-1.7b",
  "messages": [
    {"role": "user", "content": "Say hello in one word."}
  ],
  "max_tokens": 10
}'

response=$(curl -s -w "\nHTTP_CODE:%{http_code}" \
    -X POST \
    -H "Content-Type: application/json" \
    -d "$request_body" \
    "$SLM_SERVER_URL/v1/chat/completions" || echo "HTTP_CODE:000")

http_code=$(echo "$response" | grep "HTTP_CODE" | cut -d: -f2)
body=$(echo "$response" | sed '/HTTP_CODE/d')

if [ "$http_code" = "200" ]; then
    echo "✅ Chat completions passed"
    echo "$body" | python3 -m json.tool 2>/dev/null || echo "$body"

    # Extract the response text
    response_text=$(echo "$body" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('choices', [{}])[0].get('message', {}).get('content', 'N/A'))" 2>/dev/null || echo "N/A")
    echo ""
    echo "Response text: $response_text"
else
    echo "❌ Chat completions failed (HTTP $http_code)"
    echo "Response: $body"
    echo ""
    echo "Possible issues:"
    echo "  1. Backend server not running (check if model server is started)"
    echo "  2. Model not found in slm_server config"
    echo "  3. Backend server error"
    exit 1
fi
echo ""

echo "✅ All SLM Server tests passed!"
echo ""
echo "Next: Test the agent with:"
echo "  python -m pytest tests/test_llm_client/ -v"
