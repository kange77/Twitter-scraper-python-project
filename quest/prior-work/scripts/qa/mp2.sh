V=/tmp/claude-0/venv/bin; export PYTHONPATH=/home/user/Twitter-scraper-python-project
for procs in 1 2 4; do rm -f job5.db*
 s=$(date +%s.%N); $V/python run8767.py crawl job5.db -i seeds5.txt --rate 100000 --workers 256 --processes $procs --progress 0 --no-adaptive > mp2_$procs.log 2>&1; e=$(date +%s.%N)
 echo "procs=$procs wall=$($V/python -c "print(round($e-$s,1))")s  $(grep -o 'job job5.*' mp2_$procs.log)"
done
