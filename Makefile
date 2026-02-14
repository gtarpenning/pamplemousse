GIST_ID := 647086fc495e95af99f9aed9011304dc

gist:
	gh gist edit $(GIST_ID) -a pamplemousse.py

publish:
	@VERSION=$$(grep '^version' pyproject.toml | sed 's/.*"\(.*\)"/\1/'); \
	IFS='.' read -r MAJOR MINOR PATCH <<< "$$VERSION"; \
	NEW="$$MAJOR.$$MINOR.$$((PATCH + 1))"; \
	sed -i '' "s/version = \"$$VERSION\"/version = \"$$NEW\"/" pyproject.toml; \
	echo "$$VERSION -> $$NEW"
	rm -rf dist
	uv build
	uv publish --token $$(awk -F' *= *' '/password/{print $$2}' ~/.pypirc)

release: gist publish
