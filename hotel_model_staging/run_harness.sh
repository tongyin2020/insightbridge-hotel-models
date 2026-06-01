#!/bin/bash
export PATH="/opt/anaconda3/bin:/usr/local/bin:/usr/bin:/bin"
cd /Users/tongyin/hotel_model_staging
python3 /Users/tongyin/hotel_model_staging/run_21d_harness.py \
    >> /Users/tongyin/hotel_model_staging/logs/launchd.out.log \
    2>> /Users/tongyin/hotel_model_staging/logs/launchd.err.log
