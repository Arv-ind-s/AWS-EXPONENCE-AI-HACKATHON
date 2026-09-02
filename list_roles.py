#!/usr/bin/env python3
"""List available roles."""
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from covenant_radar.db.models.identity import Role
from covenant_radar.config.settings import get_settings

settings = get_settings()
database_url = settings.database.url

engine = create_engine(database_url, pool_pre_ping=True)
session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

with session_factory() as session:
    roles = session.scalars(select(Role)).all()
    print("Available roles:")
    for role in roles:
        print(f"  - {role.code}: {role.name}")
