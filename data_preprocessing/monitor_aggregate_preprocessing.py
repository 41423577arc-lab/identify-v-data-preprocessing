"""Live console monitor for run_aggregate_preprocessing.py."""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path,help='正在生成的新 SQLite 数据库路径')
    parser.add_argument('--interval',type=float,default=2.0)
    args=parser.parse_args()
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    uri=f'file:{args.database.resolve().as_posix()}?mode=ro'
    last=None
    while True:
        try:
            db=sqlite3.connect(uri,uri=True,timeout=2)
            row=db.execute('SELECT status,phase,completed,total,excluded_posts,source_longtext_markers,updated_at,detail FROM preprocessing_run WHERE singleton=1').fetchone()
            db.close()
            if row:
                status,phase,done,total,excluded,longtext,updated,detail=row
                percent=(done/total*100) if total else 100
                line=f'{updated} | {status} | {phase} | {done}/{total} ({percent:.1f}%) | 超话过滤微博 {excluded} | 汇总库可识别平台长文标记 {longtext}'
                if line!=last: print(line,flush=True);last=line
                if detail and phase=='complete': print('统计:',detail,flush=True)
                if status in ('complete','failed'): return
            else:
                print('等待处理器初始化进度表…',flush=True)
        except sqlite3.OperationalError:
            print('等待输出数据库创建…',flush=True)
        time.sleep(max(0.5,args.interval))

if __name__=='__main__': main()
