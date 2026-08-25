FROM mysql:8.0

# Copy the compressed dump to MySQL's init directory.
# Files in this directory are automatically imported on first container start.
COPY db/nouns_morf.sql.gz /docker-entrypoint-initdb.d/

ENV MYSQL_ROOT_PASSWORD=password
ENV MYSQL_DATABASE=runouns
