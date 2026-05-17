.PHONY: dev install

install:
	pip install -r requirements.txt

dev:
	@test -f .env || (echo "ERROR: .env not found. Run: cp .env.example .env" && exit 1)
	uvicorn main:app --reload
