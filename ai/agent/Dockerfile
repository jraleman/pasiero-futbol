# The match agent: agent.py (standard library) and its team of agents (team.py: Microsoft Agent Framework on Azure
# OpenAI). The code is mounted at /p4n4 (ai/docker-compose.yml), so only the packages are baked in.
FROM python:3.13-slim
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
