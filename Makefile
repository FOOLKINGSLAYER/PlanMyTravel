.PHONY: install create-db init-db seed sync-images run test

install:
	python -m pip install -r requirements.txt

create-db:
	python scripts/create_turso_db.py

init-db:
	python scripts/init_db.py

seed: init-db

sync-images:
	python scripts/sync_images.py

run:
	flask --app run.py run --debug

test:
	python -m pytest
