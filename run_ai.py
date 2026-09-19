import argparse
from app.ai_studio import serve


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='独立 DeepSeek / ChatGPT AI 助手，本机服务')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    serve(args.port)
