"""启动 Gradio 应用"""
import subprocess
import sys


def main():
    """主函数"""
    print("🚀 启动 Quart 量化研究平台 (Gradio)...")
    print("📍 访问地址: http://localhost:7860")
    print("📍 如需局域网访问: http://<your-ip>:7860")
    print("-" * 50)
    
    # 启动 Gradio 应用
    subprocess.run([sys.executable, "app.py"], check=True)


if __name__ == "__main__":
    main()
