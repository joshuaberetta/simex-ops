FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
# pg_dump for Settings → Database dump (ops/dbdump.py). It must be at least the server's major
# version (17); Debian's own client is older, so it comes from the PostgreSQL apt repository.
ARG PG_MAJOR=17
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && . /etc/os-release \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-${PG_MAJOR} \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
 && pg_dump --version
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
# gthread with one thread and one connection per worker behaves like sync workers (as many
# requests at once, as many database connections; a busy worker takes no more connections), but
# its timeout means a hung worker, not a long response: downloading a large backup or database
# dump (Settings, streamed as it is made) isn't killed partway.
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py seed_simex && exec gunicorn simex.wsgi --bind 0.0.0.0:8080 --workers 3 --worker-class gthread --threads 1 --worker-connections 1 --keep-alive 0 --timeout 120 --access-logfile -"]
