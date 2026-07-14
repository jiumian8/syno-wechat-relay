FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY app.py .

# 暴露端口
EXPOSE 5000

# 启动命令
CMD ["python", "app.py"]