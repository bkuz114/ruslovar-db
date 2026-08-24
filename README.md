# ruslovar-db

Modified Russian noun morphology database for use with [ruslovar-api](https://github.com/bkuz114/ruslovar-api).

## Source

The data originates from the [sshra/database-russian-morphology](https://github.com/sshra/database-russian-morphology) project, with modifications applied for use with ruslovar-api.

## Quickstart

Clone the repo:

```bash
git clone https://github.com/bkuz114/ruslovar-db.git
cd ruslovar-db
```

Uncompress the dump:

```bash
gzip -d db/nouns_morf.sql.gz
```

Create the database and import:

```bash
mysql -u root -p -e "CREATE DATABASE runouns;"
mysql -u root -p runouns < db/nouns_morf.sql
```

Once imported, the database is ready for use with ruslovar-api. See the [ruslovar-api documentation](https://github.com/bkuz114/ruslovar-api) for full setup instructions.

## License

Data originates from the Sshra project. See their repository for license information.
