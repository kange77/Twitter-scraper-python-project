V=/tmp/claude-0/venv/bin; export PYTHONPATH=/home/user/Twitter-scraper-python-project
(nohup $V/python /home/user/Twitter-scraper-python-project/benchmarks/mock_x.py --port 8768 --latency 0.02 --limit 300 --window 3 > mock3.log 2>&1 &); sleep 1
sed 's/8766/8768/' run8766.py > run8768.py
seq 6000000 6003000 > s6.txt
for procs in 1 4; do rm -f j7.db*; curl -s "127.0.0.1:8768/__stats?reset" >/dev/null
 s=$(date +%s.%N); $V/python run8768.py crawl j7.db -i s6.txt --rate 1000 --workers 128 --processes $procs --progress 0 > rl_$procs.log 2>&1; e=$(date +%s.%N)
 echo "procs=$procs wall=$($V/python -c "print(round($e-$s,1))")s stats=$(curl -s 127.0.0.1:8768/__stats)"; grep -o 'job j7.*' rl_$procs.log; grep -c WARNING rl_$procs.log
done
