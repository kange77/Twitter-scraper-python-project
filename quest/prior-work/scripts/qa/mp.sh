V=/tmp/claude-0/venv/bin; export PYTHONPATH=/home/user/Twitter-scraper-python-project
ok() { curl -s 127.0.0.1:8767/__stats | $V/python -c "import sys,json;print(json.load(sys.stdin)['ok'])"; }
run() { procs=$1; rate=$2; workers=$3
 rm -f job3.db*; curl -s "127.0.0.1:8767/__stats?reset" >/dev/null
 $V/python run8767.py crawl job3.db -i seeds3.txt --rate $rate --workers $workers --processes $procs --progress 0 > c3.log 2>&1 &
 P=$!; sleep 3; a=$(ok); t0=$(date +%s.%N); sleep 6; b=$(ok); t1=$(date +%s.%N)
 echo "processes=$procs rate=$rate workers=$workers: observed $($V/python -c "print(round(($b-$a)/($t1-$t0)))") req/s"
 kill -INT -- -$P 2>/dev/null; kill -INT $P; wait $P 2>/dev/null; sleep 1; pgrep -f xscraper-crawl >/dev/null && pkill -9 -f "spawn_main" ; sleep 1
}
set -m
#run 4 100 32
run 1 3000 256
run 4 3000 256
