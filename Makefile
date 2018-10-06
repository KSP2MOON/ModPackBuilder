
env: env/bin/activate

env/bin/activate: requirements.txt
	test -d env || virtualenv env -p /usr/local/bin/python3
	env/bin/pip install -Ur requirements.txt
	touch env/bin/activate

run:
	rm -rf .cache build
	env/bin/python build.py 2>&1 | tee build.log

run_debug:
	rm -rf .cache build
	env/bin/python build.py 2>&1 | tee build.log
	less build.log
