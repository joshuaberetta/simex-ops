FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Collected with production settings so the hashed-name manifest matches what runs.
RUN PUBLIC_URL=https://build.invalid SECRET_KEY=build-only python manage.py collectstatic --noinput \
 && useradd --system --create-home --uid 10001 simex && chown -R simex /srv
USER simex
EXPOSE 8080
# web: migrate, make sure the ExCon login exists, then serve. The scheduler runs as a
# separate component with: python manage.py run_scheduler
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py seed_simex && exec gunicorn simex.wsgi --bind 0.0.0.0:8080 --workers 3 --timeout 120 --access-logfile -"]
