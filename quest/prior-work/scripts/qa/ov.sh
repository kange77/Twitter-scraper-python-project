V=/tmp/claude-0/venv/bin; export PYTHONPATH=/home/user/Twitter-scraper-python-project
(nohup $V/python /home/user/Twitter-scraper-python-project/benchmarks/mock_x.py --port 8769 --latency 0.05 --overload 32 > mock4.log 2>&1 &); sleep 1
sed 's/8766/8769/' run8766.py > run8769.py
seq 7000000 7005000 > s7.txt
for flag in "" "--no-adaptive"; do rm -f j8.db*; curl -s "127.0.0.1:8769/__stats?reset" >/dev/null
 s=$(date +%s.%N); $V/python run8769.py crawl j8.db -i s7.txt --rate 5000 --workers 256 $flag --progress 0 > ov.log 2>&1; e=$(date +%s.%N)
 echo "adaptive=${flag:-on} wall=$($V/python -c "print(round($e-$s,1))")s stats=$(curl -s 127.0.0.1:8769/__stats)"; grep -o 'job j8.*' ov.log
done
