from alembic import context


def run_migrations():
    connection = context.config.attributes.get("connection")
    if connection is None:
        raise RuntimeError("Migrations require the configured Database connection")
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
