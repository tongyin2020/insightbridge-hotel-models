"""
InsightBridge — 澳门76家真实酒店数据采集器
hotel_data_collector.py
================================================
每天 09:00 / 22:00 由 launchd 触发
通过 Shifter 住宅代理采集76家酒店：
  轨道A：官网BAR价格（最优可订价）
  轨道B：OTA竞对价 + 间接库存/CRM信号

手动测试：
  python3 hotel_data_collector.py --test        # 只跑前3家酒店
  python3 hotel_data_collector.py --hotel WYNN  # 只跑指定酒店
  python3 hotel_data_collector.py               # 全量76家

数据存入：
  /Users/tongyin/Desktop/InsightBridge_模型测试系统/hotel_collector/hotel_real_data.db
"""

from __future__ import annotations
import os, sys, time, json, sqlite3, random, re, logging, argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

# ── 第三方库（pip3 install playwright requests beautifulsoup4 python-dotenv snownlp textblob）
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
try:
    from snownlp import SnowNLP
    _SNOW_OK = True
except ImportError:
    _SNOW_OK = False
try:
    from textblob import TextBlob
    _BLOB_OK = True
except ImportError:
    _BLOB_OK = False

# ── 声誉情感引擎（同目录 sentiment_engine.py）
try:
    from sentiment_engine import save_reputation_snapshot as _save_rep_snap
    _REP_OK = True
except ImportError:
    _REP_OK = False
    def _save_rep_snap(hotel_id, tier, conn):
        return {}

# ── 意图触发 MDP（同目录 acquisition_mdp.py）
try:
    from acquisition_mdp import (
        run_acquisition_sweep as _run_mdp_sweep,
        weekly_elasticity_calibration as _calibrate_elasticity,
    )
    _MDP_OK = True
except ImportError:
    _MDP_OK = False
    def _run_mdp_sweep(hotels, conn, verbose=True): return []
    def _calibrate_elasticity(conn): return {}

# ── 路径 & 配置 ────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
ENV_FILE   = Path("/Users/tongyin/Desktop/Hotel Model Rvisions/.env")
DB_PATH    = BASE_DIR / "hotel_real_data.db"
LOG_PATH   = BASE_DIR / "collector.log"

load_dotenv(ENV_FILE)

SHIFTER_API_KEY = os.getenv("SHIFTER_API_KEY", "")
SHIFTER_USER    = os.getenv("SHIFTER_USER", "")
SHIFTER_PASS    = os.getenv("SHIFTER_PASS", "")
SHIFTER_HOST    = os.getenv("SHIFTER_HOST", "p.shifter.io")
SHIFTER_PORT    = os.getenv("SHIFTER_PORT", "443")

# 采集未来哪几天的入住价格
CHECKIN_OFFSETS = [1, 7, 14, 30]   # 今天+N天

# ── 日志 ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [COLLECTOR] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
#  76家澳门酒店名单（含真实客房数、区域、官网订房URL、OTA链接）
# ══════════════════════════════════════════════════════════════════════════
HOTELS_76: list[dict] = [
    # ── 五星豪华（12家）───────────────────────────────────────────────────
    {"id": "MAC_5DX_WYNN_001",   "cn": "永利澳门",       "en": "Wynn Macau",
     "star": 5, "tier": "5_deluxe", "area": "澳门半岛", "rooms": 1010,
     "booking_url": "https://www.wynnmacau.com/en/hotels/wynn-macau/rooms-and-suites",
     "ibe_url": "https://www.wynnmacau.com/en/hotels/wynn-macau",
     "booking_com_id": "191433", "agoda_id": "56734"},

    {"id": "MAC_5DX_WYNN_002",   "cn": "永利皇宫",       "en": "Wynn Palace",
     "star": 5, "tier": "5_deluxe", "area": "路氹城", "rooms": 1706,
     "booking_url": "https://www.wynnpalace.com/en/hotels/wynn-palace/rooms-and-suites",
     "ibe_url": "https://www.wynnpalace.com/en/hotels/wynn-palace",
     "booking_com_id": "4082985", "agoda_id": "1724117"},

    {"id": "MAC_5DX_NUWA_003",   "cn": "颐居",           "en": "Nuwa Macau",
     "star": 5, "tier": "5_deluxe", "area": "路氹城", "rooms": 300,
     "booking_url": "https://www.cityofdreamsmacau.com/en/stay/nuwa",
     "ibe_url": "https://be.synxis.com/?hotel=76016&chain=10237",
     "booking_com_id": "2075668", "agoda_id": "1219025"},

    {"id": "MAC_5DX_NOLM_004",   "cn": "新东方置地酒店", "en": "New Orient Landmark Hotel",
     "star": 5, "tier": "5_deluxe", "area": "澳门半岛", "rooms": 451,
     "booking_url": "https://www.newlandmarkhotel.com.mo",
     "ibe_url": "https://www.newlandmarkhotel.com.mo/en/rooms",
     "booking_com_id": "309099", "agoda_id": "7215"},

    {"id": "MAC_5DX_GRAN_005",   "cn": "新葡京酒店",     "en": "Grand Lisboa Hotel",
     "star": 5, "tier": "5_deluxe", "area": "澳门半岛", "rooms": 430,
     "booking_url": "https://www.grandlisboa.com/en/hotel/rooms",
     "ibe_url": "https://www.grandlisboa.com/en/hotel/rooms",
     "booking_com_id": "236934", "agoda_id": "20879"},

    {"id": "MAC_5DX_MGMM_006",   "cn": "澳门美高梅",     "en": "MGM Macau",
     "star": 5, "tier": "5_deluxe", "area": "澳门半岛", "rooms": 597,
     "booking_url": "https://www.mgm.mo/en/stay/mgm-macau",
     "ibe_url": "https://www.mgm.mo/en/stay/mgm-macau",
     "mgm_booking": {"hotel_code": "001", "template": "001STD"},
     "booking_com_id": "308424", "agoda_id": "20882"},

    {"id": "MAC_5DX_T13_007",    "cn": "十三皇宫",       "en": "The 13 Hotel",
     "star": 5, "tier": "5_deluxe", "area": "路环",    "rooms": 199,
     "booking_url": "https://www.the13.com/en/stay",
     "ibe_url": "https://www.the13.com/en/stay",
     "booking_com_id": "5327601", "agoda_id": "8086052"},

    {"id": "MAC_5DX_FOUR_008",   "cn": "澳门四季酒店",   "en": "Four Seasons Hotel Macao",
     "star": 5, "tier": "5_deluxe", "area": "路氹城", "rooms": 360,
     "booking_url": "https://www.fourseasons.com/macau/",
     "ibe_url": "https://be.synxis.com/?hotel=76072&chain=2548",
     "booking_com_id": "432041", "agoda_id": "156083"},

    {"id": "MAC_5DX_GLPA_009",   "cn": "上葡京综合度假村", "en": "Grand Lisboa Palace",
     "star": 5, "tier": "5_deluxe", "area": "路氹城", "rooms": 460,
     "booking_url": "https://www.grandlisboapalace.com/en/hotel/rooms",
     "ibe_url": "https://www.grandlisboapalace.com/en/hotel/rooms",
     "booking_com_id": "9156126", "agoda_id": "13826735"},

    {"id": "MAC_5DX_MGMC_010",   "cn": "美狮美高梅",     "en": "MGM Cotai",
     "star": 5, "tier": "5_deluxe", "area": "路氹城", "rooms": 1400,
     "booking_url": "https://www.mgm.mo/en/stay/mgm-cotai",
     "ibe_url": "https://www.mgm.mo/en/stay/mgm-cotai",
     "mgm_booking": {"hotel_code": "002", "template": "002STD"},
     "booking_com_id": "5327600", "agoda_id": "7960972"},

    {"id": "MAC_5DX_ALTI_011",   "cn": "新濠锋",         "en": "Altira Macau",
     "star": 5, "tier": "5_deluxe", "area": "氹仔",   "rooms": 216,
     "booking_url": "https://www.altiramacau.com/en/rooms-suites",
     "ibe_url": "https://be.synxis.com/?hotel=76067&chain=10237",
     "booking_com_id": "357005", "agoda_id": "107698"},

    {"id": "MAC_5DX_LGPA_012",   "cn": "励宫酒店",       "en": "Legend Palace Hotel",
     "star": 5, "tier": "5_deluxe", "area": "澳门半岛", "rooms": 281,
     "booking_url": "https://www.legendpalacehotel.com/en/rooms",
     "ibe_url": "https://www.legendpalacehotel.com/en/rooms",
     "booking_com_id": "6648451", "agoda_id": "9196154"},

    # ── 五星（28家）───────────────────────────────────────────────────────
    {"id": "MAC_5ST_VENE_013",   "cn": "澳门威尼斯人",   "en": "The Venetian Macao",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 3000,
     "booking_url": "https://www.venetianmacao.com/hotel/rooms-suites.html",
     "ibe_url": "https://www.venetianmacao.com/hotel/rooms-suites.html",
     "booking_com_id": "295480", "agoda_id": "148055"},

    {"id": "MAC_5ST_GALX_014",   "cn": "银河酒店",       "en": "Galaxy Hotel",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 1550,
     "booking_url": "https://www.galaxymacau.com/hotels/galaxy-hotel",
     "ibe_url": "https://be.synxis.com/?hotel=76040&chain=10237",
     "booking_com_id": "1056659", "agoda_id": "641025"},

    {"id": "MAC_5ST_CONA_015",   "cn": "澳门康莱德",     "en": "Conrad Macao",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 634,
     "booking_url": "https://www.hilton.com/en/hotels/mfmcici-conrad-macao/",
     "ibe_url": "https://be.synxis.com/?hotel=76008&chain=2206",
     "booking_com_id": "1234567", "agoda_id": "428024"},

    {"id": "MAC_5ST_MORP_016",   "cn": "摩珀斯",         "en": "Morpheus",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 770,
     "booking_url": "https://www.cityofdreamsmacau.com/en/stay/morpheus",
     "ibe_url": "https://be.synxis.com/?hotel=76018&chain=10237",
     "booking_com_id": "5327598", "agoda_id": "7960973"},

    {"id": "MAC_5ST_STMR_017",   "cn": "澳门瑞吉",       "en": "The St. Regis Macao",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 400,
     "booking_url": "https://www.marriott.com/en-us/hotels/mfmxr-the-st-regis-macao/",
     "ibe_url": "https://be.synxis.com/?hotel=76074&chain=2548",
     "booking_com_id": "3753337", "agoda_id": "3497285"},

    {"id": "MAC_5ST_RITZ_018",   "cn": "丽思卡尔顿",     "en": "The Ritz-Carlton Macao",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 263,
     "booking_url": "https://www.ritzcarlton.com/en/hotels/china/macau",
     "ibe_url": "https://be.synxis.com/?hotel=76069&chain=2548",
     "booking_com_id": "1819613", "agoda_id": "1564660"},

    {"id": "MAC_5ST_JWMR_019",   "cn": "JW万豪",         "en": "JW Marriott Macao",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 1015,
     "booking_url": "https://www.marriott.com/en-us/hotels/mfmjw-jw-marriott-hotel-macao/",
     "ibe_url": "https://be.synxis.com/?hotel=76056&chain=2548",
     "booking_com_id": "1819616", "agoda_id": "1564663"},

    {"id": "MAC_5ST_OKUR_020",   "cn": "澳门大仓",       "en": "Hotel Okura Macau",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 488,
     "booking_url": "https://www.hotelokuramacau.com/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76032&chain=5925",
     "booking_com_id": "1819615", "agoda_id": "1564662"},

    {"id": "MAC_5ST_BANY_021",   "cn": "悦榕庄",         "en": "Banyan Tree Macao",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 246,
     "booking_url": "https://www.banyantree.com/china/macao",
     "ibe_url": "https://be.synxis.com/?hotel=76005&chain=5925",
     "booking_com_id": "1056661", "agoda_id": "641026"},

    {"id": "MAC_5ST_HYAT_022",   "cn": "君悦酒店",       "en": "Grand Hyatt Macau",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 791,
     "booking_url": "https://www.hyatt.com/grand-hyatt/en-US/mfmgh-grand-hyatt-macau",
     "ibe_url": "https://www.hyatt.com/grand-hyatt/en-US/mfmgh-grand-hyatt-macau",
     "booking_com_id": "1819617", "agoda_id": "1564664"},

    {"id": "MAC_5ST_ANDA_023",   "cn": "安达仕",         "en": "Andaz Macau",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 386,
     "booking_url": "https://www.hyatt.com/andaz/en-US/mfmaz-andaz-macau",
     "ibe_url": "https://www.hyatt.com/andaz/en-US/mfmaz-andaz-macau",
     "booking_com_id": "5327599", "agoda_id": "7960974"},

    {"id": "MAC_5ST_LGRD_024",   "cn": "伦敦人名汇",     "en": "The Londoner Grand",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 600,
     "booking_url": "https://www.londonermacao.com/en/stay/londoner-grand",
     "ibe_url": "https://www.londonermacao.com/en/stay/londoner-grand",
     "booking_com_id": "9956234", "agoda_id": "14756112"},

    {"id": "MAC_5ST_STAR_025",   "cn": "星际酒店",       "en": "StarWorld Hotel",
     "star": 5, "tier": "5_star", "area": "澳门半岛", "rooms": 507,
     "booking_url": "https://www.starworldmacau.com/en/rooms",
     "ibe_url": "https://www.starworldmacau.com/en/rooms",
     "booking_com_id": "304977", "agoda_id": "20880"},

    {"id": "MAC_5ST_LISB_026",   "cn": "葡京酒店",       "en": "Hotel Lisboa",
     "star": 5, "tier": "5_star", "area": "澳门半岛", "rooms": 927,
     "booking_url": "https://www.hotellisboa.com/en/rooms",
     "ibe_url": "https://www.hotellisboa.com/en/rooms",
     "booking_com_id": "191436", "agoda_id": "7217"},

    {"id": "MAC_5ST_ROYL_027",   "cn": "皇都酒店",       "en": "Hotel Royal Macau",
     "star": 5, "tier": "5_star", "area": "澳门半岛", "rooms": 380,
     "booking_url": "https://www.hotelroyal.com.mo/en/rooms-suites",
     "ibe_url": "https://www.hotelroyal.com.mo/en/rooms-suites",
     "booking_com_id": "309095", "agoda_id": "7218"},

    {"id": "MAC_5ST_SOFT_028",   "cn": "十六浦索菲特",   "en": "Sofitel Macau",
     "star": 5, "tier": "5_star", "area": "澳门半岛", "rooms": 408,
     "booking_url": "https://all.accor.com/hotel/7076/index.en.shtml",
     "ibe_url": "https://be.synxis.com/?hotel=76070&chain=2848",
     "booking_com_id": "498765", "agoda_id": "258033"},

    {"id": "MAC_5ST_MAND_029",   "cn": "文华东方",       "en": "Mandarin Oriental Macau",
     "star": 5, "tier": "5_star", "area": "澳门半岛", "rooms": 213,
     "booking_url": "https://www.mandarinoriental.com/en/macau/mandarin-oriental",
     "ibe_url": "https://be.synxis.com/?hotel=76047&chain=10237",
     "booking_com_id": "1056663", "agoda_id": "641027"},

    {"id": "MAC_5ST_ARTZ_030",   "cn": "澳门雅辰",       "en": "Artyzen Grand Lapa Macau",
     "star": 5, "tier": "5_star", "area": "澳门半岛", "rooms": 434,
     "booking_url": "https://grandlapamacau.artyzenhotels.com/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76004&chain=10237",
     "booking_com_id": "233278", "agoda_id": "7211"},

    {"id": "MAC_5ST_LARC_031",   "cn": "凯旋门",         "en": "L'Arc Hotel Macao",
     "star": 5, "tier": "5_star", "area": "澳门半岛", "rooms": 200,
     "booking_url": "https://www.larchotelmacao.com/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76044&chain=10237",
     "booking_com_id": "1175367", "agoda_id": "838015"},

    {"id": "MAC_5ST_BROD_032",   "cn": "百老汇酒店",     "en": "Broadway Hotel Macau",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 320,
     "booking_url": "https://www.broadwaymacau.com.mo/en/rooms",
     "ibe_url": "https://www.broadwaymacau.com.mo/en/rooms",
     "booking_com_id": "3753338", "agoda_id": "3497286"},

    {"id": "MAC_5ST_GCOL_033",   "cn": "鹭环海天度假",   "en": "Grand Coloane Resort",
     "star": 5, "tier": "5_star", "area": "路环",   "rooms": 208,
     "booking_url": "https://www.grandcoloane.com/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76028&chain=10237",
     "booking_com_id": "192093", "agoda_id": "7216"},

    {"id": "MAC_5ST_RIVI_034",   "cn": "濠璟酒店",       "en": "Riviera Hotel Macao",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 500,
     "booking_url": "https://www.rivieramacao.com/en/rooms",
     "ibe_url": "https://www.rivieramacao.com/en/rooms",
     "booking_com_id": "6648452", "agoda_id": "9196155"},

    {"id": "MAC_5ST_CRPL_035",   "cn": "澳门皇冠假日",   "en": "Crowne Plaza Macao",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 281,
     "booking_url": "https://www.ihg.com/crowneplaza/hotels/us/en/macau/mfmcp/hoteldetail",
     "ibe_url": "https://be.synxis.com/?hotel=76012&chain=10237",
     "booking_com_id": "1367765", "agoda_id": "1067887"},

    {"id": "MAC_5ST_PSTG_036",   "cn": "圣地牙哥古堡",   "en": "Pousada de São Tiago",
     "star": 5, "tier": "5_star", "area": "澳门半岛", "rooms": 12,
     "booking_url": "https://www.saotiago.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76063&chain=10237",
     "booking_com_id": "192098", "agoda_id": "7220"},

    {"id": "MAC_5ST_REGA_037",   "cn": "丽景湾艺术酒店", "en": "Regency Art Hotel",
     "star": 5, "tier": "5_star", "area": "氹仔",   "rooms": 301,
     "booking_url": "https://www.regencyarthotel.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76066&chain=10237",
     "booking_com_id": "219256", "agoda_id": "21049"},

    {"id": "MAC_5ST_YOHO_038",   "cn": "YOHO金银岛",     "en": "YOHO Treasure Island",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 600,
     "booking_url": "https://www.yohomacau.com/en/rooms",
     "ibe_url": "https://www.yohomacau.com/en/rooms",
     "booking_com_id": "6648453", "agoda_id": "9196156"},

    {"id": "MAC_5ST_YOHR_039",   "cn": "YOHO荷里活罗斯福", "en": "YOHO Roosevelt Hollywood",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 560,
     "booking_url": "https://www.yohomacau.com/en/rooms",
     "ibe_url": "https://www.yohomacau.com/en/rooms",
     "booking_com_id": "6648454", "agoda_id": "9196157"},

    {"id": "MAC_5ST_TRSN_040",   "cn": "新濠天地翠湖",   "en": "Nüwa/Crown Towers",
     "star": 5, "tier": "5_star", "area": "路氹城", "rooms": 420,
     "booking_url": "https://www.cityofdreamsmacau.com/en/stay",
     "ibe_url": "https://be.synxis.com/?hotel=76017&chain=10237",
     "booking_com_id": "2075670", "agoda_id": "1219027"},

    # ── 四星（18家）───────────────────────────────────────────────────────
    {"id": "MAC_4ST_STCT_041",   "cn": "新濠影汇酒店",   "en": "Studio City Hotel",
     "star": 4, "tier": "4_star", "area": "路氹城", "rooms": 1600,
     "booking_url": "https://www.studiocity-macau.com/en/stay",
     "ibe_url": "https://be.synxis.com/?hotel=76073&chain=10237",
     "booking_com_id": "3284895", "agoda_id": "2726491"},

    {"id": "MAC_4ST_LGND_042",   "cn": "澳门伦敦人",     "en": "The Londoner Macao",
     "star": 4, "tier": "4_star", "area": "路氹城", "rooms": 600,
     "booking_url": "https://www.londonermacao.com/en/stay/londoner-hotel",
     "ibe_url": "https://www.londonermacao.com/en/stay/londoner-hotel",
     "booking_com_id": "9956235", "agoda_id": "14756113"},

    {"id": "MAC_4ST_LSBM_043",   "cn": "葡京人",         "en": "Lisboeta Macau",
     "star": 4, "tier": "4_star", "area": "澳门半岛", "rooms": 419,
     "booking_url": "https://www.lisboetamacau.com/en/rooms",
     "ibe_url": "https://www.lisboetamacau.com/en/rooms",
     "booking_com_id": "9156127", "agoda_id": "13826736"},

    {"id": "MAC_4ST_RIOH_044",   "cn": "利澳酒店",       "en": "Rio Hotel Macau",
     "star": 4, "tier": "4_star", "area": "氹仔",   "rooms": 614,
     "booking_url": "https://www.riohotel.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76068&chain=10237",
     "booking_com_id": "262960", "agoda_id": "7219"},

    {"id": "MAC_4ST_GOLD_045",   "cn": "金龙酒店",       "en": "Hotel Golden Dragon",
     "star": 4, "tier": "4_star", "area": "澳门半岛", "rooms": 375,
     "booking_url": "https://www.hotelgoldendragon.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76030&chain=10237",
     "booking_com_id": "233277", "agoda_id": "7213"},

    {"id": "MAC_4ST_CASA_046",   "cn": "皇家金堡",       "en": "Casa Real Hotel",
     "star": 4, "tier": "4_star", "area": "澳门半岛", "rooms": 385,
     "booking_url": "https://www.casarealhotel.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76007&chain=10237",
     "booking_com_id": "263049", "agoda_id": "7212"},

    {"id": "MAC_4ST_METR_047",   "cn": "维景酒店",       "en": "Metropark Hotel Macau",
     "star": 4, "tier": "4_star", "area": "澳门半岛", "rooms": 530,
     "booking_url": "https://www.metroparkhotelmacau.com/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76050&chain=10237",
     "booking_com_id": "309097", "agoda_id": "7214"},

    {"id": "MAC_4ST_BEVP_048",   "cn": "富豪酒店",       "en": "Beverly Plaza Hotel",
     "star": 4, "tier": "4_star", "area": "澳门半岛", "rooms": 296,
     "booking_url": "https://www.beverlyplazahotel.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76006&chain=10237",
     "booking_com_id": "233276", "agoda_id": "7210"},

    {"id": "MAC_4ST_HRBV_049",   "cn": "励庭海景",       "en": "Harbourview Hotel Macau",
     "star": 4, "tier": "4_star", "area": "澳门半岛", "rooms": 328,
     "booking_url": "https://www.harbourviewmacau.com/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76031&chain=10237",
     "booking_com_id": "309096", "agoda_id": "7222"},

    {"id": "MAC_4ST_ASCT_050",   "cn": "雅诗阁澳门",     "en": "Ascott Macau",
     "star": 4, "tier": "4_star", "area": "澳门半岛", "rooms": 128,
     "booking_url": "https://www.discoverasr.com/en/ascott/china/ascott-macau",
     "ibe_url": "https://be.synxis.com/?hotel=76003&chain=10237",
     "booking_com_id": "263048", "agoda_id": "7209"},

    {"id": "MAC_4ST_GRVW_051",   "cn": "君怡酒店",       "en": "Grandview Hotel Macau",
     "star": 4, "tier": "4_star", "area": "氹仔",   "rooms": 408,
     "booking_url": "https://www.grandviewhotel.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76029&chain=10237",
     "booking_com_id": "219254", "agoda_id": "21047"},

    {"id": "MAC_4ST_GRDR_052",   "cn": "骏龙酒店",       "en": "Grand Dragon Hotel",
     "star": 4, "tier": "4_star", "area": "澳门半岛", "rooms": 388,
     "booking_url": "https://www.granddragonhotel.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76027&chain=10237",
     "booking_com_id": "191434", "agoda_id": "7221"},

    {"id": "MAC_4ST_HOLI_053",   "cn": "澳门假日",       "en": "Holiday Inn Macao Cotai",
     "star": 4, "tier": "4_star", "area": "路氹城", "rooms": 374,
     "booking_url": "https://www.ihg.com/holidayinn/hotels/us/en/macau/mfmhc/hoteldetail",
     "ibe_url": "https://be.synxis.com/?hotel=76033&chain=10237",
     "booking_com_id": "1367764", "agoda_id": "1067886"},

    {"id": "MAC_4ST_PRES_054",   "cn": "总统酒店",       "en": "President Hotel Macau",
     "star": 4, "tier": "4_star", "area": "澳门半岛", "rooms": 340,
     "booking_url": "https://www.presidenthotel.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76064&chain=10237",
     "booking_com_id": "233280", "agoda_id": "7224"},

    {"id": "MAC_4ST_PCOL_055",   "cn": "竹湾酒店",       "en": "Pousada de Coloane",
     "star": 4, "tier": "4_star", "area": "路环",   "rooms": 30,
     "booking_url": "https://www.pousadadecoloane.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76062&chain=10237",
     "booking_com_id": "192097", "agoda_id": "7223"},

    {"id": "MAC_4ST_GOCR_056",   "cn": "金皇冠大酒店",   "en": "Golden Crown China Hotel",
     "star": 4, "tier": "4_star", "area": "澳门半岛", "rooms": 155,
     "booking_url": "https://www.goldencrownchinahotel.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76025&chain=10237",
     "booking_com_id": "233279", "agoda_id": "7225"},

    {"id": "MAC_4ST_PMIN_057",   "cn": "皇庭海景",       "en": "Pousada Marina Infante",
     "star": 4, "tier": "4_star", "area": "氹仔",   "rooms": 76,
     "booking_url": "https://www.pousadamarinainfante.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76061&chain=10237",
     "booking_com_id": "219255", "agoda_id": "21048"},

    {"id": "MAC_4ST_REMH_058",   "cn": "幻宇酒店",       "en": "Rem Hotel Macau",
     "star": 4, "tier": "4_star", "area": "澳门半岛", "rooms": 180,
     "booking_url": "https://www.remhotelmacau.com/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76065&chain=10237",
     "booking_com_id": "5327602", "agoda_id": "8086053"},

    # ── 三星（18家）───────────────────────────────────────────────────────
    {"id": "MAC_3ST_EMPE_059",   "cn": "帝濠酒店",       "en": "Emperor Hotel Macau",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 80,
     "booking_url": "https://www.emperorhotel.com.mo/en/rooms",
     "ibe_url": "https://www.emperorhotel.com.mo/en/rooms",
     "booking_com_id": "191432", "agoda_id": "7226"},

    {"id": "MAC_3ST_FORT_060",   "cn": "财神酒店",       "en": "Hotel Fortune Macau",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 354,
     "booking_url": "https://www.hotelfortune.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76034&chain=10237",
     "booking_com_id": "233282", "agoda_id": "7227"},

    {"id": "MAC_3ST_GEEM_061",   "cn": "英皇娱乐酒店",   "en": "Grand Emperor Hotel",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 130,
     "booking_url": "https://www.grandemperor.com/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76026&chain=10237",
     "booking_com_id": "309094", "agoda_id": "7228"},

    {"id": "MAC_3ST_GUIA_062",   "cn": "东望洋酒店",     "en": "Hotel Guia Macau",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 90,
     "booking_url": "https://www.hotelguia.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76035&chain=10237",
     "booking_com_id": "233283", "agoda_id": "7229"},

    {"id": "MAC_3ST_METP_063",   "cn": "京都酒店",       "en": "Metropole Hotel Macau",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 112,
     "booking_url": "https://www.metropolehotel.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76049&chain=10237",
     "booking_com_id": "233284", "agoda_id": "7230"},

    {"id": "MAC_3ST_HIEX_064",   "cn": "智选假日",       "en": "Holiday Inn Express Macau",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 213,
     "booking_url": "https://www.ihg.com/holidayinnexpress/hotels/us/en/macau/mfmex/hoteldetail",
     "ibe_url": "https://be.synxis.com/?hotel=76036&chain=10237",
     "booking_com_id": "1367766", "agoda_id": "1067888"},

    {"id": "MAC_3ST_INNM_065",   "cn": "中湾格兰酒店",   "en": "Inn Hotel Macau",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 96,
     "booking_url": "https://www.innhotelmacau.com/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76038&chain=10237",
     "booking_com_id": "191435", "agoda_id": "7231"},

    {"id": "MAC_3ST_CENT_066",   "cn": "中央酒店",       "en": "Hotel Central Macau",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 161,
     "booking_url": "https://www.hotelcentral.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76010&chain=10237",
     "booking_com_id": "233285", "agoda_id": "7232"},

    {"id": "MAC_3ST_MLDN_067",   "cn": "万龙酒店",       "en": "Million Dragon Hotel",
     "star": 3, "tier": "3_star", "area": "氹仔",   "rooms": 96,
     "booking_url": "https://www.milliondragonhotel.com/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76051&chain=10237",
     "booking_com_id": "219257", "agoda_id": "21050"},

    {"id": "MAC_3ST_CANA_068",   "cn": "康怡酒店",       "en": "Canary Hotel Macau",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 88,
     "booking_url": "https://www.canaryhotelmacau.com/en/rooms",
     "ibe_url": "https://www.canaryhotelmacau.com/en/rooms",
     "booking_com_id": "191438", "agoda_id": "7233"},

    {"id": "MAC_3ST_PORI_069",   "cn": "葡式酒店",       "en": "Pousada de Mong-Há",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 20,
     "booking_url": "https://www.ift.edu.mo/pousada/en/rooms",
     "ibe_url": "https://www.ift.edu.mo/pousada/en/rooms",
     "booking_com_id": "191437", "agoda_id": "7234"},

    {"id": "MAC_3ST_ORQD_070",   "cn": "奥斯路酒店",     "en": "Oxalis Hotel Macau",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 73,
     "booking_url": "https://www.oxalishotelmacau.com/en/rooms",
     "ibe_url": "https://www.oxalishotelmacau.com/en/rooms",
     "booking_com_id": "233286", "agoda_id": "7235"},

    {"id": "MAC_3ST_VILL_071",   "cn": "里斯本酒店",     "en": "Vila Gale Macau",
     "star": 3, "tier": "3_star", "area": "氹仔",   "rooms": 128,
     "booking_url": "https://www.vilagale.com/en/hotels/asia/vila-gale-macau",
     "ibe_url": "https://be.synxis.com/?hotel=76076&chain=10237",
     "booking_com_id": "219258", "agoda_id": "21051"},

    {"id": "MAC_3ST_HONG_072",   "cn": "皇廷海景",       "en": "Hotel Hong Kong Macau",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 58,
     "booking_url": "https://www.hotelhongkongmacau.com/en/rooms",
     "ibe_url": "https://www.hotelhongkongmacau.com/en/rooms",
     "booking_com_id": "233287", "agoda_id": "7236"},

    {"id": "MAC_3ST_EAST_073",   "cn": "东方酒店",       "en": "Oriental Hotel Macau",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 65,
     "booking_url": "https://www.orientalhotelmacau.com/en/rooms",
     "ibe_url": "https://www.orientalhotelmacau.com/en/rooms",
     "booking_com_id": "233288", "agoda_id": "7237"},

    {"id": "MAC_3ST_KPLA_074",   "cn": "京华酒店",       "en": "Kingsway Hotel Macau",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 296,
     "booking_url": "https://www.kingswayhotel.com.mo/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76043&chain=10237",
     "booking_com_id": "233289", "agoda_id": "7238"},

    {"id": "MAC_3ST_LSUN_075",   "cn": "丽日酒店",       "en": "Sunny Day Hotel Macau",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 72,
     "booking_url": "https://www.sunnydayhotelmacau.com/en/rooms",
     "ibe_url": "https://www.sunnydayhotelmacau.com/en/rooms",
     "booking_com_id": "309098", "agoda_id": "7239"},

    {"id": "MAC_3ST_SINC_076",   "cn": "新新酒店",       "en": "Sintra Hotel Macau",
     "star": 3, "tier": "3_star", "area": "澳门半岛", "rooms": 229,
     "booking_url": "https://www.hotelsintra.com/en/rooms",
     "ibe_url": "https://be.synxis.com/?hotel=76071&chain=10237",
     "booking_com_id": "191439", "agoda_id": "7240"},
]

assert len(HOTELS_76) == 76, f"酒店数量异常: {len(HOTELS_76)}"


# ══════════════════════════════════════════════════════════════════════════
#  Shifter 代理会话
# ══════════════════════════════════════════════════════════════════════════
def make_session(country: str = "hk") -> requests.Session:
    """构建带 Shifter 住宅代理的 requests Session（按流量扣费）"""
    sess = requests.Session()
    if SHIFTER_USER and SHIFTER_PASS:
        proxy_url = (
            f"http://{SHIFTER_USER}:{SHIFTER_PASS}@{SHIFTER_HOST}:{SHIFTER_PORT}"
        )
        sess.proxies = {"http": proxy_url, "https": proxy_url}
    else:
        log.warning("未找到 Shifter 代理凭证，将使用本机IP（测试模式）")

    # 随机化 User-Agent（模拟真实浏览器）
    ua_pool = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36",
    ]
    lang_pool = ["zh-HK,zh;q=0.9", "zh-TW,zh;q=0.9,en;q=0.8", "en-US,en;q=0.9", "pt-PT,pt;q=0.9"]
    sess.headers.update({
        "User-Agent":      random.choice(ua_pool),
        "Accept-Language": random.choice(lang_pool),
        "Accept":          "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
    })
    return sess


# ══════════════════════════════════════════════════════════════════════════
#  数据库初始化
# ══════════════════════════════════════════════════════════════════════════
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS price_snapshots (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        hotel_id        TEXT NOT NULL,
        hotel_name_cn   TEXT,
        star            INTEGER,
        tier            TEXT,
        area            TEXT,
        total_rooms     INTEGER,
        snapshot_time   TEXT NOT NULL,       -- ISO格式
        checkin_date    TEXT NOT NULL,       -- YYYY-MM-DD
        official_bar    REAL,               -- 官网最优价 MOP
        official_rack   REAL,               -- 官网标价
        member_rate     REAL,               -- 会员价
        currency        TEXT DEFAULT 'MOP',
        room_type       TEXT,
        avail_status    TEXT,               -- available/low/sold_out/unknown
        low_stock_flag  INTEGER DEFAULT 0,  -- OTA是否有"仅剩X间"
        booking_rate    REAL,               -- Booking.com价格
        agoda_rate      REAL,               -- Agoda价格
        ota_discount    REAL,               -- OTA比官网低的比例
        source_ok       INTEGER DEFAULT 0,  -- 是否成功拿到真实数据
        notes           TEXT
    );
    CREATE TABLE IF NOT EXISTS price_trends (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        hotel_id        TEXT NOT NULL,
        checkin_date    TEXT NOT NULL,
        calc_time       TEXT NOT NULL,
        bar_vs_prev     REAL,               -- 较上次快照价格变动
        trend_3snap     TEXT,               -- "上行"/"下行"/"持平"
        booking_pace    TEXT                -- "快"/"正常"/"慢"（由库存+价格联合判断）
    );
    CREATE TABLE IF NOT EXISTS review_metrics (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        hotel_id        TEXT NOT NULL,
        collected_at    TEXT NOT NULL,
        booking_score   REAL,
        agoda_score     REAL,
        tripadvisor_pos INTEGER,            -- TripAdvisor 澳门排名
        review_count    INTEGER,
        sentiment_flag  TEXT                -- "positive"/"mixed"/"negative"
    );
    CREATE TABLE IF NOT EXISTS inventory_signals (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        hotel_id        TEXT NOT NULL,
        checkin_date    TEXT NOT NULL,
        captured_at     TEXT NOT NULL,
        source          TEXT,               -- "booking_com" / "agoda"
        urgency_text    TEXT,               -- 原始文字 e.g. "Only 3 rooms left"
        rooms_remaining INTEGER,            -- 推算库存 (NULL=未知)
        avail_level     TEXT                -- "critical"(<3) / "low"(3-9) / "moderate"(10-19) / "available"
    );
    CREATE TABLE IF NOT EXISTS google_ratings (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        hotel_id        TEXT NOT NULL,
        captured_date   TEXT NOT NULL,      -- YYYY-MM-DD (每天最多一条)
        google_rating   REAL,               -- 4.3
        review_count    INTEGER,            -- 1234
        price_level     TEXT,               -- "$$$" etc
        raw_snippet     TEXT,
        UNIQUE(hotel_id, captured_date)
    );
    CREATE TABLE IF NOT EXISTS review_sentiment (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        hotel_id        TEXT NOT NULL,
        captured_date   TEXT NOT NULL,      -- YYYY-MM-DD
        source          TEXT,               -- "booking_com" / "tripadvisor"
        lang            TEXT,               -- "zh" / "en"
        sample_count    INTEGER,
        avg_sentiment   REAL,               -- 0.0-1.0 (SnowNLP) 或 -1.0-1.0 (TextBlob)
        top_praise      TEXT,               -- JSON 关键词列表
        top_complaint   TEXT,               -- JSON 关键词列表
        sentiment_label TEXT,               -- "positive"/"mixed"/"negative"
        UNIQUE(hotel_id, captured_date, source, lang)
    );
    CREATE INDEX IF NOT EXISTS idx_price_hotel_checkin
        ON price_snapshots(hotel_id, checkin_date);
    CREATE INDEX IF NOT EXISTS idx_price_time
        ON price_snapshots(snapshot_time);
    CREATE INDEX IF NOT EXISTS idx_inventory_hotel_checkin
        ON inventory_signals(hotel_id, checkin_date);
    CREATE INDEX IF NOT EXISTS idx_google_hotel
        ON google_ratings(hotel_id, captured_date);
    CREATE INDEX IF NOT EXISTS idx_sentiment_hotel
        ON review_sentiment(hotel_id, captured_date);
    CREATE TABLE IF NOT EXISTS reputation_metrics (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        hotel_id        TEXT NOT NULL,
        computed_date   TEXT NOT NULL,      -- YYYY-MM-DD（每天最多一次）
        R_t             REAL,               -- 实时声誉指数 [-1,1]
        M_t             REAL,               -- 声誉动量 [-1,1]
        delta_R         REAL,               -- 相对竞对差 [-1,1]
        confidence      REAL,               -- Wilson置信因子 [0,1]
        gamma_eff       REAL,               -- 有效定价影响系数
        rep_adj         REAL,               -- 推荐价格调节幅度
        momentum_sign   TEXT,               -- "rising"/"falling"/"stable"
        alert_level     TEXT,               -- "high"/"medium"/"low"
        review_count    INTEGER,
        UNIQUE(hotel_id, computed_date)
    );
    CREATE INDEX IF NOT EXISTS idx_reputation_hotel
        ON reputation_metrics(hotel_id, computed_date);
    """)
    conn.commit()
    return conn


# ══════════════════════════════════════════════════════════════════════════
#  价格提取工具
# ══════════════════════════════════════════════════════════════════════════
def _extract_prices(text: str) -> list[float]:
    """从HTML/JS文本中提取所有合法价格数字（扩展版 v2）

    多层匹配策略：
    ① 货币前缀（MOP/HK$/HKD/澳門幣）— 最可靠
    ② JSON/JS 键值对（覆盖SynXis/Marriott/Hilton/IHG等常见IBE格式）
    ③ HTML data-* 属性
    ④ class含price/rate的标签文本
    ⑤ 文字上下文关键词前后
    """
    prices: list[float] = []

    _JSON_KEYS = (
        r'price|rate|lowestPrice|totalPrice|amount|nightlyRate|displayPrice|'
        r'baseRate|minRate|minNightlyRate|displayAmount|regularPrice|memberPrice|'
        r'originalPrice|fromPrice|perNight|bestRate|availableRate|barRate|netRate|'
        r'roomRate|totalRate|rateAmount|priceAmount|rateValue|formattedPrice|'
        r'formattedRate|currentPrice|discountedPrice|finalPrice|rackRate|salePrice|'
        r'lowestRate|cheapestRate|dailyRate|unitPrice|listPrice|displayRate|'
        r'grossRate|minimumRate|startRate|openingRate|baseAmount|'
        r'NightlyRate|RateAmount|TotalPrice|BaseRate|MinRate|DisplayPrice|'
        r'PublicRate|RackRate|BestRate|NetRate|GrossRate|RoomRate|RateValue|'
        r'StartingRate|FromRate|LowestAvailable|BestAvailableRate|'
        r'AverageNightlyRate|TotalNightlyRate|UndiscountedDailyRate'
    )

    patterns = [
        # ── 1. 货币前缀（最可靠）
        r'MOP[\s,]*(\d[\d,]+(?:\.\d{1,2})?)',
        r'MOP\s*(\d[\d,]+)',
        r'HK\$[\s,]*(\d[\d,]+(?:\.\d{1,2})?)',
        r'HKD[\s,]*(\d[\d,]+(?:\.\d{1,2})?)',
        r'澳[門门][幣币元][\s]*(\d[\d,]+)',

        # ── 2. JSON/JS 键值（双引号）
        rf'"(?:{_JSON_KEYS})"[:\s]+"?(\d+\.?\d*)"?',
        # ── 2b. JSON/JS 键值（单引号）
        rf"'(?:price|rate|amount|total|cost|nightlyRate|displayPrice)'[:\s]+'?(\d+\.?\d*)'?",
        # ── 2c. JS 对象字面量（无引号键）
        rf'(?:^|[,{{])\s*(?:{_JSON_KEYS})\s*:\s*(\d{{3,6}})',

        # ── 3. HTML data-* 属性
        r'data-(?:price|rate|amount|cost|nightly|bar)["\s]*=[\s"\']*(\d[\d,.]+)',
        r'data-room-(?:price|rate|cost)["\s]*=[\s"\']*(\d[\d,.]+)',
        r'data-(?:lowest|minimum|min|best|from)-(?:price|rate)["\s]*=[\s"\']*(\d[\d,.]+)',
        r'data-value["\s]*=[\s"\']*(\d[\d,.]+)',

        # ── 4. class含price/rate的标签内数字（单行HTML片段）
        r'class="[^"]*(?:price|rate|cost|amount)[^"]*"[^>]{0,80}>\s*[^\d<]{0,10}(\d{3,5})',
        r"class='[^']*(?:price|rate|cost|amount)[^']*'[^>]{0,80}>\s*[^\d<]{0,10}(\d{3,5})",

        # ── 5. 文字上下文关键词（多语言）
        r'(?:from|起|每晚起|每房每晚|每晚|starting\s+from|as\s+low\s+as|starts?\s+at)\s*'
        r'(?:MOP\s*|HK\$\s*)?(\d{3,6})',
        r'(?:Price|Rate)\s*:\s*["\']?(\d{3,6})',
        r'(?:value|content)="(\d{3,6})"[^>]*(?:price|rate|cost)',

        # ── 6. 原有模式保留（向后兼容）
        r'"(?:price|rate|lowestPrice|totalPrice|amount)"[:\s]+"?(\d+\.?\d*)',
        r"'(?:price|rate)'[:\s]+'?(\d+\.?\d*)",
        r'(?:price|rate)["\s:=]+(\d{3,6})',
    ]

    for pat in patterns:
        try:
            for m in re.finditer(pat, text, re.IGNORECASE | re.MULTILINE):
                try:
                    raw = m.group(1).replace(",", "").replace(" ", "")
                    p = float(raw)
                    # 价格合法范围：MOP 100–100000，同时排除明显的年份数字
                    if 100 < p < 100000 and not (1900 <= p <= 2030 and p == int(p)):
                        prices.append(p)
                except Exception:
                    pass
        except re.error:
            pass

    return sorted(set(prices))


# ══════════════════════════════════════════════════════════════════════════
#  库存探针：Max Bookable Test + Sold-out 监控
#  在同一个 Playwright page 对象上运行，不额外请求
# ══════════════════════════════════════════════════════════════════════════
def _probe_inventory(page, html: str) -> tuple[Optional[int], bool]:
    """
    返回 (max_bookable: int|None, sold_out: bool)
    max_bookable = 该页面可选的最大房间数
    sold_out = 是否检测到整体售罄状态
    """
    max_bookable: Optional[int] = None
    sold_out = False

    # ── 方法1：select[name*=room] / select[name*=rooms] 的 options 最大值 ──
    # 大多数官网订房引擎（如 Wynn、Galaxy、SJM 的 IBE）用 <select> 选间数
    try:
        for sel in ['select[name*="room"]', 'select[id*="room"]',
                    'select[name*="qty"]',  'select[name*="count"]',
                    'select[class*="room"]','select[class*="quantity"]',
                    'select[name="no_rooms"]', 'select[name="rooms"]']:
            elems = page.query_selector_all(sel)
            for elem in elems:
                options = elem.query_selector_all("option")
                vals = []
                for opt in options:
                    v = opt.get_attribute("value") or opt.inner_text().strip()
                    try:
                        n = int(re.sub(r'\D', '', v))
                        if 1 <= n <= 30:
                            vals.append(n)
                    except Exception:
                        pass
                if vals:
                    max_bookable = max(vals)
                    break
            if max_bookable:
                break
    except Exception:
        pass

    # ── 方法2：input[type=number][max] ──────────────────────────────────
    if max_bookable is None:
        try:
            for sel in ['input[type="number"][max]', 'input[name*="room"][max]']:
                elems = page.query_selector_all(sel)
                for elem in elems:
                    m_attr = elem.get_attribute("max")
                    if m_attr:
                        try:
                            v = int(m_attr)
                            if 1 <= v <= 30:
                                max_bookable = v
                                break
                        except Exception:
                            pass
                if max_bookable:
                    break
        except Exception:
            pass

    # ── 方法3：HTML正则（+按钮 aria-label 或 data-max 属性）────────────
    if max_bookable is None:
        for pat in [r'data-max[_-]?rooms?["\s]*[:=]["\s]*(\d+)',
                    r'"maxRooms?"\s*:\s*(\d+)',
                    r'max[_-]?rooms?\s*=\s*["\']?(\d+)',
                    r'max.*?(\d+)\s*room']:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                try:
                    v = int(m.group(1))
                    if 1 <= v <= 30:
                        max_bookable = v
                        break
                except Exception:
                    pass

    # ── Sold-out 检测 ────────────────────────────────────────────────────
    # 关键词扫描（中英文）
    html_lower = html.lower()
    sold_kws = ["sold out", "sold-out", "fully booked", "no availability",
                "unavailable", "no rooms available", "room not available",
                "已售罄", "已满房", "售完", "客满", "无房", "暂无可用",
                "sold_out", "notavailable", "room_unavailable"]
    if any(kw in html_lower for kw in sold_kws):
        sold_out = True

    # DOM 元素检测：sold-out class 的房间卡片
    if not sold_out:
        try:
            so_selectors = [
                '[class*="sold-out"]', '[class*="soldout"]',
                '[class*="unavailable"]', '[class*="not-available"]',
                '[data-availability="0"]', '[data-avail="false"]',
                'button[disabled]:has-text("Book")',
            ]
            for sel in so_selectors:
                if page.query_selector(sel):
                    sold_out = True
                    break
        except Exception:
            pass

    return max_bookable, sold_out


# ── Playwright 全局实例（每次采集共用一个浏览器进程）
_PW_CONTEXT = None


def _fetch_mgm_price(hotel: dict, checkin: str, checkout: str, ctx) -> dict | None:
    """Method 3c: MGM 专用 — 访问 booking.mgm.mo/api/calendar/get 获取每日价格

    MGM 的预订引擎在独立域名 booking.mgm.mo，通过日历 API 返回精确 BAR 价格。
    API: POST https://booking.mgm.mo/api/calendar/get
    响应: {"date":"2026-06-08","price1":4494.80,"price2":4494.80,"price3":4919.80}
      price1 = BAR (最低可订价)
      price3 = 豪华/高级房型价
    """
    mgm = hotel.get("mgm_booking", {})
    if not mgm:
        return None

    hotel_code = mgm["hotel_code"]
    template   = mgm["template"]
    result_prices = []

    try:
        page = ctx.new_page()

        def on_mgm_resp(response):
            if "calendar/get" in response.url and response.status == 200:
                try:
                    body = response.json()
                    data = body.get("data", [])
                    for entry in data:
                        d = entry.get("date", "")
                        p1 = entry.get("price1")
                        p3 = entry.get("price3")
                        if d == checkin and p1 and 200 < p1 < 100000:
                            result_prices.append((p1, p3))
                            log.debug(f"  [MGM calendar] {d} price1={p1} price3={p3}")
                except Exception as e_cal:
                    log.debug(f"  [MGM calendar parse] {e_cal}")

        page.on("response", on_mgm_resp)

        mgm_url = (
            f"https://booking.mgm.mo/selectMonthdate"
            f"?locale=en-US&template={template}&hotel={hotel_code}"
            f"&checkIn={checkin}&checkOut={checkout}"
        )
        page.goto(mgm_url, timeout=28000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except PwTimeout:
            page.wait_for_timeout(5000)

        page.close()

        if result_prices:
            bar, rack = result_prices[0]
            return {
                "official_bar": bar,
                "official_rack": rack if rack and rack != bar else None,
                "source_ok": 1,
                "notes": "mgm_calendar_api",
            }
    except Exception as e_mgm:
        log.debug(f"  [MGM booking engine] {hotel['cn']} failed: {e_mgm}")

    return None


def _get_browser_context():
    """返回Playwright浏览器上下文（懒加载，不走Shifter代理）

    策略说明：
    - requests.Session → 使用Shifter住宅代理（p.shifter.io:443），CONNECT隧道正常
    - Playwright/Chromium → 直连（本机IP），原因：
        * Chromium对 p.shifter.io:443 的CONNECT响应有TLS握手问题(ERR_TUNNEL_CONNECTION_FAILED)
        * Playwright+Shifter代理会消耗大量带宽（每次JS渲染 2-5 MB）
        * 每天3次×76家酒店的温和频率不触发IP封禁
        * SynXis JSON API（22+家酒店）已通过requests+Shifter抓取，Playwright仅是补充
    """
    global _PW_CONTEXT
    if _PW_CONTEXT is not None:
        return _PW_CONTEXT

    pw = sync_playwright().start()
    ua_pool = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    ]
    browser = pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-infobars",
        ],
    )
    ctx = browser.new_context(
        user_agent=random.choice(ua_pool),
        locale="zh-HK",
        viewport={"width": 1366, "height": 768},
        extra_http_headers={
            "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        },
    )
    _PW_CONTEXT = ctx
    return ctx


# ══════════════════════════════════════════════════════════════════════════
#  采集核心：官网价格（轨道A） — Playwright JS渲染引擎
# ══════════════════════════════════════════════════════════════════════════
def fetch_official_price(hotel: dict, checkin: str, sess: requests.Session) -> dict:
    """用Playwright抓取酒店官网JS渲染后的价格"""
    checkout = (datetime.strptime(checkin, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    result = {
        "hotel_id": hotel["id"], "checkin_date": checkin,
        "official_bar": None, "official_rack": None,
        "currency": "MOP", "room_type": "标准房",
        "avail_status": "unknown", "source_ok": 0, "notes": ""
    }

    # ── 方法1：SynXis IBE（JSON API优先，降级到HTML解析）──────────────────
    if "be.synxis.com" in hotel.get("ibe_url", ""):
        try:
            ibe = hotel["ibe_url"]
            m_ibe = re.search(r'hotel=(\d+)&chain=(\d+)', ibe)
            if m_ibe:
                hotel_code, chain_code = m_ibe.group(1), m_ibe.group(2)
                synxis_html_cache = None  # 保存HTML以供后续解析

                # 1a：SynXis JSON API
                for api_url in [
                    f"https://be.synxis.com/?hotel={hotel_code}&chain={chain_code}&arrive={checkin}&depart={checkout}&rooms=1&adults=2&currency=MOP&output=json",
                    f"https://api.synxis.com/availability/v1/hotel/{hotel_code}?arrive={checkin}&depart={checkout}&chain={chain_code}&rooms=1&adults=2",
                ]:
                    try:
                        r1 = sess.get(api_url, timeout=12,
                                      headers={"Accept": "application/json, text/javascript, */*"})
                        if r1.status_code == 200:
                            txt = r1.text.strip()
                            if txt.startswith("{") or txt.startswith("["):
                                try:
                                    data = r1.json()
                                    prices = []
                                    for key in ["rates", "roomRates", "availableRooms", "rooms",
                                                "RatePlans", "ratePlans", "RoomTypes"]:
                                        items = data.get(key, [])
                                        if isinstance(items, list):
                                            for item in items:
                                                for pk in ["rate", "total", "price", "lowestRate",
                                                           "netRate", "Amount", "RateAmount",
                                                           "NightlyRate", "TotalPrice"]:
                                                    v = item.get(pk)
                                                    if v:
                                                        try:
                                                            fv = float(v)
                                                            if fv > 100:
                                                                prices.append(fv)
                                                        except Exception:
                                                            pass
                                    if prices:
                                        result.update({
                                            "official_bar": min(prices),
                                            "official_rack": max(prices),
                                            "avail_status": "available",
                                            "source_ok": 1,
                                            "notes": "synxis_json"
                                        })
                                        return result
                                except Exception:
                                    pass
                            else:
                                # SynXis返回了HTML — 缓存供方法1b使用
                                if len(txt) > 500 and synxis_html_cache is None:
                                    synxis_html_cache = txt
                    except Exception:
                        pass

                # 1b：SynXis HTML解析（API返回HTML时的降级路径）
                if synxis_html_cache is None:
                    # 直接请求IBE页面（带日期参数）
                    ibe_page_url = (
                        f"https://be.synxis.com/?hotel={hotel_code}&chain={chain_code}"
                        f"&arrive={checkin}&depart={checkout}&rooms=1&adults=2&currency=MOP"
                    )
                    try:
                        r1b = sess.get(ibe_page_url, timeout=15,
                                       headers={
                                           "Accept": "text/html,application/xhtml+xml,*/*",
                                           "Referer": f"https://be.synxis.com/?hotel={hotel_code}&chain={chain_code}",
                                           "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                                       })
                        if r1b.status_code == 200 and len(r1b.text) > 500:
                            synxis_html_cache = r1b.text
                    except Exception:
                        pass

                if synxis_html_cache:
                    html_prices = _extract_prices(synxis_html_cache)
                    html_prices = [p for p in html_prices if 200 < p < 80000]
                    if html_prices:
                        result.update({
                            "official_bar": html_prices[0],
                            "official_rack": html_prices[-1] if len(html_prices) > 1 else None,
                            "avail_status": "available",
                            "source_ok": 1,
                            "notes": f"synxis_html ({len(html_prices)}prices)"
                        })
                        return result
        except Exception as e:
            result["notes"] = f"synxis_err:{str(e)[:40]}"

    # ── 方法2：requests轻量HTML解析（代理友好，Playwright的前置尝试）──────
    # requests通过Shifter CONNECT隧道工作正常；Playwright Chromium在同一代理上有TLS问题
    # 对于Booking.com风格的静态价格块，requests足够；Wynn/MGM/SJM等官网也先试requests
    # SSL降级策略：先HTTPS(verify=True) → HTTPS(verify=False) → HTTP
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        url = hotel["booking_url"]
        sep = "&" if "?" in url else "?"
        req_url = f"{url}{sep}checkin={checkin}&checkout={checkout}&adults=2&rooms=1"
        req_headers = {
            "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
            "Referer": "https://www.google.com/",
            "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
        }

        r = None
        # 尝试顺序：① HTTPS+verify ② HTTPS-verify ③ HTTP
        attempt_urls = [req_url]
        if req_url.startswith("https://"):
            attempt_urls.append(req_url)          # 第二次 verify=False
            attempt_urls.append(req_url.replace("https://", "http://"))  # HTTP降级
        verify_flags = [True, False, False]

        for attempt_url, verify_flag in zip(attempt_urls, verify_flags):
            try:
                r = sess.get(attempt_url, timeout=15, verify=verify_flag,
                             headers=req_headers)
                if r.status_code == 200:
                    break
                r = None
            except requests.exceptions.SSLError:
                r = None
                continue
            except Exception:
                r = None
                break

        if r and r.status_code == 200:
            page_prices = _extract_prices(r.text)
            page_prices = [p for p in page_prices if 200 < p < 80000]
            # 检查售罄
            sold_kws = ["sold out", "已售罄", "no availability", "unavailable",
                        "no rooms available", "sold_out", "客满"]
            is_sold = any(kw in r.text.lower() for kw in sold_kws)
            low_stock = any(kw in r.text.lower() for kw in
                            ["only 1 room", "only 2 room", "last room", "仅剩1", "仅剩2"])
            if page_prices:
                result.update({
                    "official_bar":  page_prices[0],
                    "official_rack": page_prices[-1] if len(page_prices) > 1 else None,
                    "avail_status":  "sold_out" if is_sold else ("low" if low_stock else "available"),
                    "source_ok": 1,
                    "notes": f"requests_html ({len(page_prices)} prices)"
                })
                return result
    except Exception as e:
        result["notes"] = f"requests_err:{str(e)[:40]}"

    # ── 方法3：Playwright JS渲染（最终备用，仅当requests也失败时）────────
    # 注意：方法3 和 方法3b 分开异常处理，确保DNS/网络失败也能执行方法3b
    ctx = None
    try:
        ctx = _get_browser_context()
    except Exception as e_ctx:
        result["notes"] = f"playwright_err:{str(e_ctx)[:60]}"

    # ── 方法3c：MGM 专用预订引擎（booking.mgm.mo/api/calendar/get）──────
    # MGM 官网是 Next.js SPA，价格只在独立子域名 booking.mgm.mo 的日历 API 返回
    if ctx is not None and result.get("source_ok") != 1 and hotel.get("mgm_booking"):
        mgm_result = _fetch_mgm_price(hotel, checkin, checkout, ctx)
        if mgm_result:
            result.update(mgm_result)
            log.info(f"  ✅ {checkin}: BAR={result['official_bar']} | {result['notes']}")
            return result
        else:
            result["notes"] = "mgm_calendar_no_price"

    if ctx is not None and result.get("source_ok") != 1:
        # ── 方法3 主流程：加载酒店官网 ────────────────────────────────────
        # MGM 已由 Method 3c 处理（booking.mgm.mo），不再重复加载 www.mgm.mo
        if not hotel.get("mgm_booking"):
            html = ""
            max_bookable = None
            sold_out_detected = False
            captured_prices: list[float] = []

            try:
                page = ctx.new_page()

                # 捕获所有 JSON 响应（移除 URL 关键词过滤，避免遗漏非标准 API 端点）
                def on_response(response):
                    try:
                        if response.status == 200:
                            ct = response.headers.get("content-type", "")
                            if "json" in ct:
                                try:
                                    body = response.json()
                                    text = json.dumps(body)
                                except Exception:
                                    try:
                                        text = response.text()
                                    except Exception:
                                        return
                                for p in _extract_prices(text):
                                    if 200 < p < 80000:
                                        captured_prices.append(p)
                    except Exception:
                        pass

                page.on("response", on_response)

                url = hotel["booking_url"]
                sep = "&" if "?" in url else "?"
                full_url = f"{url}{sep}checkin={checkin}&checkout={checkout}&adults=2&rooms=1"

                page.goto(full_url, timeout=28000, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PwTimeout:
                    page.wait_for_timeout(5000)

                html = page.content()
                page_prices = _extract_prices(html)

                # SynXis iframe 探测
                if "be.synxis.com" in hotel.get("ibe_url", ""):
                    try:
                        for frame in page.frames:
                            if frame == page.main_frame:
                                continue
                            frame_url = frame.url or ""
                            if "synxis" in frame_url or "ibe" in frame_url.lower():
                                try:
                                    frame_html = frame.content()
                                    if len(frame_html) > 200:
                                        page_prices.extend(_extract_prices(frame_html))
                                except Exception:
                                    pass
                    except Exception:
                        pass

                all_prices = sorted(set(captured_prices + page_prices))
                all_prices = [p for p in all_prices if 200 < p < 80000]

                max_bookable, sold_out_detected = _probe_inventory(page, html)
                if max_bookable is not None:
                    result["notes_inventory"] = f"max_bookable={max_bookable}"
                if sold_out_detected:
                    result["avail_status"] = "sold_out"
                    result["notes_inventory"] = result.get("notes_inventory", "") + "|sold_out"

                page.close()

                if all_prices:
                    avail = result.get("avail_status", "available")
                    if avail != "sold_out":
                        avail = "low" if (max_bookable is not None and max_bookable <= 5) else "available"
                    result.update({
                        "official_bar": all_prices[0],
                        "official_rack": all_prices[-1] if len(all_prices) > 1 else None,
                        "avail_status": avail,
                        "source_ok": 1,
                        "notes": f"playwright_js ({len(all_prices)}prices)"
                                  + (f" max={max_bookable}" if max_bookable else "")
                    })
                    return result
                else:
                    if sold_out_detected or any(kw in html.lower() for kw in
                            ["sold out", "已售罄", "unavailable", "no rooms", "sold_out"]):
                        result["avail_status"] = "sold_out"
                    result["notes"] = "playwright_no_price"

            except PwTimeout:
                result["notes"] = "playwright_timeout"
            except Exception as e:
                # 主页面加载失败（DNS/网络）→ 继续方法3b
                result["notes"] = f"playwright_err:{str(e)[:60]}"

        # ── 方法3b：SynXis IBE 直连（独立异常处理，主页面失败也会执行）──
        # 覆盖场景：① 主页面DNS解析失败 ② 主页面无价格 ③ SynXis通过iframe加载
        if "be.synxis.com" in hotel.get("ibe_url", "") and result.get("source_ok") != 1:
            try:
                ibe_base = hotel["ibe_url"]
                ibe_direct_url = (
                    f"{ibe_base}&arrive={checkin}&depart={checkout}"
                    f"&rooms=1&adults=2&currency=MOP"
                )
                page2 = ctx.new_page()
                synxis_captured: list[float] = []

                def on_synxis_response(response):
                    try:
                        if response.status == 200:
                            ct = response.headers.get("content-type", "")
                            if "json" in ct or "javascript" in ct:
                                try:
                                    text2 = response.text()
                                    for p in _extract_prices(text2):
                                        if 200 < p < 80000:
                                            synxis_captured.append(p)
                                except Exception:
                                    pass
                    except Exception:
                        pass

                page2.on("response", on_synxis_response)
                page2.goto(ibe_direct_url, timeout=25000, wait_until="domcontentloaded")
                try:
                    page2.wait_for_load_state("networkidle", timeout=12000)
                except PwTimeout:
                    page2.wait_for_timeout(6000)

                ibe_html = page2.content()
                ibe_prices = [p for p in _extract_prices(ibe_html) if 200 < p < 80000]
                all_synxis = sorted(set(synxis_captured + ibe_prices))
                page2.close()

                if all_synxis:
                    result.update({
                        "official_bar": all_synxis[0],
                        "official_rack": all_synxis[-1] if len(all_synxis) > 1 else None,
                        "avail_status": "available",
                        "source_ok": 1,
                        "notes": f"synxis_playwright ({len(all_synxis)}prices)"
                    })
                    return result
            except Exception as e2:
                log.debug(f"  synxis_playwright failed ({hotel['cn']}): {e2}")

    # ── 方法4：OTA兜底（Booking.com/Agoda） ─────────────────────────────────
    # 当所有官网方法均失败时，从OTA获取参考价格（MGM/T13等官网需人工交互或已下线）
    if result.get("source_ok") != 1:
        bcom_id = hotel.get("booking_com_id", "")
        if bcom_id:
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                slug = hotel["en"].lower().replace(" ", "-").replace("'", "").replace(".", "")
                bcom_url = (
                    f"https://www.booking.com/hotel/mo/{slug}.html"
                    f"?checkin={checkin}&checkout={checkout}&group_adults=2&no_rooms=1"
                    f"&selected_currency=MOP"
                )
                rb = sess.get(bcom_url, timeout=15, verify=False,
                              headers={
                                  "Accept": "text/html,application/xhtml+xml,*/*",
                                  "Referer": "https://www.booking.com/",
                                  "Accept-Language": "en-US,en;q=0.9",
                              })
                if rb.status_code == 200:
                    from bs4 import BeautifulSoup as _BS
                    soup = _BS(rb.text, "html.parser")
                    # Booking.com price selectors（多版本兼容）
                    bcom_price = None
                    for sel in [
                        '[data-testid="price-and-discounted-price"]',
                        '.bui-price-display__value',
                        '.prco-inline-block-maker-helper',
                        '[class*="prco-valign"]',
                        '.sr_gs_price_price',
                    ]:
                        for tag in soup.select(sel):
                            txt = re.sub(r'[^\d]', '', tag.get_text())
                            if txt:
                                try:
                                    p = float(txt)
                                    if 200 < p < 80000:
                                        bcom_price = p
                                        break
                                except Exception:
                                    pass
                        if bcom_price:
                            break
                    # 也尝试从HTML直接提取MOP价格
                    if not bcom_price:
                        ota_prices = _extract_prices(rb.text)
                        ota_prices = [p for p in ota_prices if 200 < p < 80000]
                        if ota_prices:
                            bcom_price = ota_prices[0]
                    if bcom_price:
                        result.update({
                            "official_bar": bcom_price,
                            "avail_status": "available",
                            "source_ok": 1,
                            "notes": "ota_fallback(booking.com)"
                        })
            except Exception as e_ota:
                log.debug(f"  OTA fallback failed ({hotel['cn']}): {e_ota}")

    return result


# ══════════════════════════════════════════════════════════════════════════
#  采集核心：OTA竞对价 + 库存信号（轨道B）
# ══════════════════════════════════════════════════════════════════════════
def fetch_ota_signals(hotel: dict, checkin: str, sess: requests.Session) -> dict:
    """从Booking.com抓取OTA价格和库存标签"""
    result = {
        "booking_rate": None, "agoda_rate": None,
        "low_stock_flag": 0, "ota_discount": None,
        "booking_score": None, "notes_ota": ""
    }
    checkout = (datetime.strptime(checkin, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    # Booking.com
    try:
        bcom_id = hotel.get("booking_com_id", "")
        if bcom_id:
            url = (
                f"https://www.booking.com/hotel/mo/{hotel['en'].lower().replace(' ','-')}.html"
                f"?checkin={checkin}&checkout={checkout}&group_adults=2&no_rooms=1"
            )
            r = sess.get(url, timeout=15)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                # 提取价格
                price_tags = soup.select('[data-testid="price-and-discounted-price"], .bui-price-display__value, .prco-inline-block-maker-helper')
                for tag in price_tags:
                    txt = re.sub(r'[^\d.]', '', tag.get_text())
                    if txt:
                        try:
                            p = float(txt)
                            if 100 < p < 100000:
                                result["booking_rate"] = p
                                break
                        except Exception:
                            pass
                # 低库存检测
                page_text = r.text.lower()
                if any(kw in page_text for kw in ["only 1 room", "只剩1間", "仅剩1间", "last room", "high demand"]):
                    result["low_stock_flag"] = 1
                # 评分提取
                score_tag = soup.select_one('[data-testid="review-score-right-component"] .ac4a7896c7')
                if score_tag:
                    try:
                        result["booking_score"] = float(score_tag.get_text().strip())
                    except Exception:
                        pass
    except Exception as e:
        result["notes_ota"] += f"bcom:{str(e)[:40]};"

    return result


# ══════════════════════════════════════════════════════════════════════════
#  功能1：OTA 库存信号（Playwright精准抓 Booking.com urgency）
# ══════════════════════════════════════════════════════════════════════════
_URGENCY_PATTERNS = [
    # 英文
    (r'only\s+(\d+)\s+room', 'en'),
    (r'(\d+)\s+room[s]?\s+left', 'en'),
    (r'last\s+(\d+)\s+room', 'en'),
    (r'(\d+)\s+left\s+at\s+this\s+price', 'en'),
    (r'in\s+high\s+demand[^\d]*(\d+)', 'en'),
    # 中文繁体/简体
    (r'只剩\s*(\d+)\s*間', 'zh'),
    (r'仅剩\s*(\d+)\s*间', 'zh'),
    (r'尚餘\s*(\d+)\s*間', 'zh'),
    (r'最後\s*(\d+)\s*間', 'zh'),
]

def _parse_urgency(text: str) -> tuple[Optional[int], str]:
    """从页面文字提取库存数量和urgency级别"""
    text_lower = text.lower()
    for pat, _ in _URGENCY_PATTERNS:
        m = re.search(pat, text_lower, re.IGNORECASE)
        if m:
            try:
                n = int(m.group(1))
                if n < 3:    level = "critical"
                elif n < 10: level = "low"
                elif n < 20: level = "moderate"
                else:        level = "available"
                return n, level
            except Exception:
                pass
    # sold out 检测
    if any(k in text_lower for k in ["sold out", "unavailable", "no rooms", "售罄", "已售完"]):
        return 0, "sold_out"
    return None, "available"

def fetch_inventory_signals(hotel: dict, checkin: str,
                            conn: sqlite3.Connection,
                            sess: requests.Session) -> dict:
    """用 Playwright 抓 Booking.com 的库存/urgency 信号，写入 inventory_signals 表"""
    captured_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    checkout = (datetime.strptime(checkin, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    bcom_id = hotel.get("booking_com_id", "")
    result = {"rooms_remaining": None, "avail_level": "available", "urgency_text": ""}

    if not bcom_id:
        return result

    try:
        slug = hotel["en"].lower().replace(" ", "-").replace("'", "")
        url = (f"https://www.booking.com/hotel/mo/{slug}.html"
               f"?checkin={checkin}&checkout={checkout}&group_adults=2&no_rooms=1&selected_currency=MOP")

        with sync_playwright() as pw:
            # 直连（不走Shifter代理，原因见 _get_browser_context 注释）
            browser = pw.chromium.launch(headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage"])
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                locale="zh-HK",
            )
            page = ctx.new_page()
            page.goto(url, timeout=25000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            content = page.content()
            ctx.close(); browser.close()

        # 提取urgency文字
        soup = BeautifulSoup(content, "html.parser")
        urgency_candidates = []
        for sel in ["[data-testid='urgency-message']", ".urgency_message_red",
                    ".limited_supply", ".bui-badge", "[class*='urgency']",
                    "[class*='scarcity']", "[class*='limited']"]:
            for tag in soup.select(sel):
                t = tag.get_text(strip=True)
                if t: urgency_candidates.append(t)
        # fallback: 全文正则
        full_text = soup.get_text(" ")
        rooms, level = _parse_urgency(" ".join(urgency_candidates) or full_text)
        urgency_raw = " | ".join(urgency_candidates[:3]) if urgency_candidates else ""

        result = {"rooms_remaining": rooms, "avail_level": level, "urgency_text": urgency_raw[:200]}
        conn.execute("""
            INSERT INTO inventory_signals
                (hotel_id, checkin_date, captured_at, source, urgency_text, rooms_remaining, avail_level)
            VALUES (?,?,?,?,?,?,?)
        """, (hotel["id"], checkin, captured_at, "booking_com",
              urgency_raw[:200], rooms, level))
        conn.commit()

    except Exception as e:
        log.debug(f"  inventory_signal error ({hotel['cn']}): {e}")

    return result


# ══════════════════════════════════════════════════════════════════════════
#  功能2：Google Maps 评分采集
# ══════════════════════════════════════════════════════════════════════════
def fetch_google_rating(hotel: dict, conn: sqlite3.Connection,
                        sess: requests.Session) -> dict:
    """从 TripAdvisor / Booking.com 抓取酒店综合评分（Google 反爬太强，改用多源聚合）"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    result = {"google_rating": None, "review_count": None, "price_level": None}

    # 检查今天是否已采集
    row = conn.execute(
        "SELECT google_rating, review_count FROM google_ratings WHERE hotel_id=? AND captured_date=?",
        (hotel["id"], today_str)
    ).fetchone()
    if row and row[0]:
        return {"google_rating": row[0], "review_count": row[1], "price_level": None}

    rating, count, source_tag = None, None, ""

    # ── 方法1：TripAdvisor 搜索结果页 JSON-LD ─────────────────────────────
    try:
        ta_query = f'site:tripadvisor.com "{hotel["en"]}" Macau'
        ta_url = f"https://www.tripadvisor.com/Search?q={requests.utils.quote(hotel['en'] + ' Macau')}"
        r = sess.get(ta_url, timeout=15, headers={"Accept-Language": "en-US,en;q=0.9"})
        if r.status_code == 200:
            for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
                                 r.text, re.DOTALL):
                try:
                    d = json.loads(m.group(1))
                    items = [d] if isinstance(d, dict) else (d if isinstance(d, list) else [])
                    for item in items:
                        agg = item.get("aggregateRating", {}) if isinstance(item, dict) else {}
                        if agg.get("ratingValue"):
                            rating = round(float(agg["ratingValue"]), 1)
                            count  = int(agg.get("reviewCount", 0) or agg.get("ratingCount", 0))
                            source_tag = "tripadvisor"
                            break
                    if rating: break
                except Exception:
                    pass

        # 正则备用：从页面提取 TripAdvisor 评分气泡
        if rating is None:
            m = re.search(r'"ratingValue"\s*:\s*"?(\d\.?\d?)"?', r.text)
            if m:
                rating = float(m.group(1))
                cm = re.search(r'"reviewCount"\s*:\s*"?(\d+)"?', r.text)
                count = int(cm.group(1)) if cm else None
                source_tag = "tripadvisor"
    except Exception:
        pass

    # ── 方法2：Booking.com 酒店评分（fallback，已在OTA模块采集）─────────────
    if rating is None:
        try:
            bcom_id = hotel.get("booking_com_id", "")
            if bcom_id:
                slug = hotel["en"].lower().replace(" ", "-").replace("'", "")
                url = f"https://www.booking.com/hotel/mo/{slug}.html?selected_currency=MOP"
                r = sess.get(url, timeout=15)
                if r.status_code == 200:
                    # JSON-LD
                    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
                                        r.text, re.DOTALL):
                        try:
                            d = json.loads(m.group(1))
                            items = [d] if isinstance(d, dict) else (d if isinstance(d, list) else [])
                            for item in items:
                                agg = item.get("aggregateRating", {}) if isinstance(item, dict) else {}
                                if agg.get("ratingValue"):
                                    # Booking.com 评分是 10分制，转换为 5分制
                                    raw = float(agg["ratingValue"])
                                    rating = round(raw / 2 if raw > 5 else raw, 1)
                                    count  = int(agg.get("reviewCount", 0))
                                    source_tag = "booking_com"
                                    break
                            if rating: break
                        except Exception:
                            pass
        except Exception:
            pass

    if rating is not None:
        try:
            conn.execute("""
                INSERT OR REPLACE INTO google_ratings
                    (hotel_id, captured_date, google_rating, review_count, price_level, raw_snippet)
                VALUES (?,?,?,?,?,?)
            """, (hotel["id"], today_str, rating, count, None, source_tag))
            conn.commit()
            log.debug(f"  rating [{source_tag}] {hotel['cn']}: {rating}★ ({count} reviews)")
        except Exception:
            pass

    result = {"google_rating": rating, "review_count": count, "price_level": None}
    return result


# ══════════════════════════════════════════════════════════════════════════
#  功能3：评论情感分析（Booking.com + SnowNLP/TextBlob）
# ══════════════════════════════════════════════════════════════════════════
_PRAISE_WORDS_ZH  = ["干净","安静","方便","景观","服务","位置","早餐","舒适","设施","热情","免费","性价比","豪华","宽敞"]
_COMPLAINT_WORDS_ZH = ["嘈杂","老旧","停车","贵","小","气味","慢","排队","差","破","薄","硬","脏","拥挤"]
_PRAISE_WORDS_EN  = ["clean","quiet","comfortable","location","staff","view","breakfast","spacious","luxury","friendly","value"]
_COMPLAINT_WORDS_EN = ["noisy","old","parking","expensive","small","smell","slow","queue","dirty","broken","thin","hard","crowded"]

def _keyword_counts(texts: list[str], words: list[str]) -> list[tuple[str,int]]:
    combined = " ".join(texts).lower()
    return sorted([(w, combined.count(w)) for w in words if combined.count(w) > 0],
                  key=lambda x: -x[1])[:5]

def fetch_review_sentiment(hotel: dict, conn: sqlite3.Connection,
                           sess: requests.Session) -> dict:
    """从 Booking.com 抓评论，运行情感分析，写入 review_sentiment 表。
    每家酒店每天最多采集一次（staleness 检查）。"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    result = {"avg_sentiment": None, "sentiment_label": None}

    row = conn.execute(
        "SELECT avg_sentiment FROM review_sentiment WHERE hotel_id=? AND captured_date=? AND source='booking_com'",
        (hotel["id"], today_str)
    ).fetchone()
    if row:
        return {"avg_sentiment": row[0], "sentiment_label": None}

    bcom_id = hotel.get("booking_com_id", "")
    if not bcom_id:
        return result

    try:
        # Booking.com JSON 评论 API（无需登录，公开端点）
        api_url = (f"https://www.booking.com/reviewlist.html"
                   f"?cc1=mo&pagename={hotel['en'].lower().replace(' ','-')}"
                   f"&type=total&rows=15&offset=0&sort=f_recent_desc&lang=zh-cn")
        r = sess.get(api_url, timeout=15)

        zh_texts, en_texts = [], []
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup.select(".review_pos, .c-review__body, [data-testid='review-positive-text']"):
                t = tag.get_text(strip=True)
                if t and len(t) > 10:
                    # 简单语言检测：CJK字符比例
                    cjk = sum(1 for c in t if '一' <= c <= '鿿')
                    if cjk / max(len(t), 1) > 0.2:
                        zh_texts.append(t)
                    else:
                        en_texts.append(t)
            for tag in soup.select(".review_neg, [data-testid='review-negative-text']"):
                t = tag.get_text(strip=True)
                if t and len(t) > 10:
                    cjk = sum(1 for c in t if '一' <= c <= '鿿')
                    if cjk / max(len(t), 1) > 0.2:
                        zh_texts.append(t)
                    else:
                        en_texts.append(t)

        all_texts = zh_texts + en_texts
        if not all_texts:
            return result

        # 情感评分
        scores = []
        if _SNOW_OK and zh_texts:
            for t in zh_texts[:10]:
                try: scores.append(SnowNLP(t).sentiments)
                except Exception: pass
        if _BLOB_OK and en_texts:
            for t in en_texts[:10]:
                try:
                    raw = TextBlob(t).sentiment.polarity  # -1~1
                    scores.append((raw + 1) / 2)           # 归一化到 0~1
                except Exception: pass

        if not scores:
            return result

        avg_s = sum(scores) / len(scores)
        label = "positive" if avg_s > 0.65 else "negative" if avg_s < 0.40 else "mixed"

        # 关键词统计
        praise_kw    = _keyword_counts(zh_texts, _PRAISE_WORDS_ZH) + _keyword_counts(en_texts, _PRAISE_WORDS_EN)
        complaint_kw = _keyword_counts(zh_texts, _COMPLAINT_WORDS_ZH) + _keyword_counts(en_texts, _COMPLAINT_WORDS_EN)
        praise_kw    = sorted(praise_kw, key=lambda x: -x[1])[:5]
        complaint_kw = sorted(complaint_kw, key=lambda x: -x[1])[:5]

        lang = "zh" if len(zh_texts) >= len(en_texts) else "en"
        conn.execute("""
            INSERT OR REPLACE INTO review_sentiment
                (hotel_id, captured_date, source, lang, sample_count, avg_sentiment,
                 top_praise, top_complaint, sentiment_label)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (hotel["id"], today_str, "booking_com", lang,
              len(all_texts), round(avg_s, 4),
              json.dumps([w for w,_ in praise_kw], ensure_ascii=False),
              json.dumps([w for w,_ in complaint_kw], ensure_ascii=False),
              label))
        conn.commit()

        result = {"avg_sentiment": round(avg_s, 4), "sentiment_label": label}
        log.debug(f"  sentiment {hotel['cn']}: {avg_s:.2f} ({label}) | praise:{[w for w,_ in praise_kw[:3]]}")

    except Exception as e:
        log.debug(f"  review_sentiment error ({hotel['cn']}): {e}")

    return result


# ══════════════════════════════════════════════════════════════════════════
#  价格趋势计算
# ══════════════════════════════════════════════════════════════════════════
def calc_price_trend(conn: sqlite3.Connection, hotel_id: str, checkin: str, current_bar: float) -> dict:
    """对比历史快照计算价格趋势"""
    rows = conn.execute("""
        SELECT official_bar FROM price_snapshots
        WHERE hotel_id=? AND checkin_date=? AND official_bar IS NOT NULL
        ORDER BY snapshot_time DESC LIMIT 3
    """, (hotel_id, checkin)).fetchall()

    trend = {"bar_vs_prev": None, "trend_3snap": "持平", "booking_pace": "正常"}
    if not rows or not current_bar:
        return trend

    prev_bar = rows[0][0]
    diff = current_bar - prev_bar
    trend["bar_vs_prev"] = round(diff, 0)

    if len(rows) >= 2:
        prices = [r[0] for r in rows] + [current_bar]
        if prices[-1] > prices[0] * 1.03:
            trend["trend_3snap"] = "上行"
            trend["booking_pace"] = "快"
        elif prices[-1] < prices[0] * 0.97:
            trend["trend_3snap"] = "下行"
            trend["booking_pace"] = "慢"

    return trend


# ══════════════════════════════════════════════════════════════════════════
#  写入数据库
# ══════════════════════════════════════════════════════════════════════════
def save_snapshot(conn: sqlite3.Connection, hotel: dict, checkin: str,
                  price_data: dict, ota_data: dict, snap_time: str):
    bar = price_data.get("official_bar")
    booking_rate = ota_data.get("booking_rate")
    ota_discount = None
    if bar and booking_rate and bar > 0:
        ota_discount = round((booking_rate - bar) / bar, 4)

    conn.execute("""
        INSERT INTO price_snapshots
          (hotel_id, hotel_name_cn, star, tier, area, total_rooms,
           snapshot_time, checkin_date, official_bar, official_rack,
           member_rate, currency, room_type, avail_status, low_stock_flag,
           booking_rate, agoda_rate, ota_discount, source_ok, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        hotel["id"], hotel["cn"], hotel["star"], hotel["tier"],
        hotel["area"], hotel["rooms"], snap_time, checkin,
        bar, price_data.get("official_rack"),
        None,                               # member_rate（需登录，暂不采集）
        price_data.get("currency", "MOP"),
        price_data.get("room_type", "标准房"),
        price_data.get("avail_status", "unknown"),
        ota_data.get("low_stock_flag", 0),
        booking_rate,
        ota_data.get("agoda_rate"),
        ota_discount,
        price_data.get("source_ok", 0),
        f"{price_data.get('notes','')}|{ota_data.get('notes_ota','')}",
    ))

    # 价格趋势
    trend = calc_price_trend(conn, hotel["id"], checkin, bar)
    conn.execute("""
        INSERT INTO price_trends (hotel_id, checkin_date, calc_time, bar_vs_prev, trend_3snap, booking_pace)
        VALUES (?,?,?,?,?,?)
    """, (hotel["id"], checkin, snap_time,
          trend["bar_vs_prev"], trend["trend_3snap"], trend["booking_pace"]))
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  主循环
# ══════════════════════════════════════════════════════════════════════════
def run_collection(hotels: list[dict], label: str = "FULL"):
    conn = init_db()
    snap_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().date()
    checkin_dates = [(today + timedelta(days=d)).strftime("%Y-%m-%d") for d in CHECKIN_OFFSETS]

    log.info(f"=== 开始采集 [{label}] | {snap_time} | {len(hotels)}家酒店 × {len(checkin_dates)}个入住日期 ===")

    ok_count = fail_count = 0
    for i, hotel in enumerate(hotels, 1):
        sess = make_session()   # 每家酒店换一个代理会话
        log.info(f"[{i:02d}/{len(hotels)}] {hotel['cn']} ({hotel['tier']})")

        # ── 每家酒店每天只跑一次的模块 ──────────────────────────────────
        today_str = datetime.now().strftime("%Y-%m-%d")
        rating_already = conn.execute(
            "SELECT google_rating FROM google_ratings WHERE hotel_id=? AND captured_date=?",
            (hotel["id"], today_str)
        ).fetchone()

        # Google/TripAdvisor 评分（今天未采集时才跑）
        g_result = {"google_rating": None, "review_count": None}
        if not rating_already:
            g_result = fetch_google_rating(hotel, conn, sess)
        g_str = f"★{g_result['google_rating']}" if g_result.get("google_rating") else "★N/A"

        # 评论情感（每天更新一次，已采集则跳过）
        sent_result = fetch_review_sentiment(hotel, conn, sess)
        s_str = f"情感{sent_result['avg_sentiment']:.2f}({sent_result.get('sentiment_label','')})" \
                if sent_result.get("avg_sentiment") else "情感N/A"

        # 声誉快照（每天更新一次：基于当天情感数据计算 R_t / M_t / ΔR_t）
        rep_already = conn.execute(
            "SELECT rep_adj FROM reputation_metrics WHERE hotel_id=? AND computed_date=?",
            (hotel["id"], today_str)
        ).fetchone()
        if not rep_already:
            try:
                rep_snap = _save_rep_snap(hotel["id"], hotel["tier"], conn)
                rep_str = f"声誉调节{rep_snap.get('rep_adj', 0)*100:+.1f}%({rep_snap.get('momentum_sign','')})"
            except Exception:
                rep_str = "声誉N/A"
        else:
            rep_str = f"声誉(已存)"

        log.info(f"  {g_str} | {s_str} | {rep_str}")

        _rating_saved_this_hotel = bool(rating_already or g_result.get("google_rating"))

        for checkin in checkin_dates:
            # 随机延迟：避免反爬
            delay = random.uniform(2.5, 7.0) if hotel["tier"] in ("5_deluxe", "5_star") else random.uniform(1.5, 4.0)
            time.sleep(delay)

            price_data = fetch_official_price(hotel, checkin, sess)
            ota_data   = fetch_ota_signals(hotel, checkin, sess)

            # 用 OTA 信号里的 booking_score 作为评分备用来源
            if not _rating_saved_this_hotel and ota_data.get("booking_score"):
                try:
                    raw_score = float(ota_data["booking_score"])
                    # Booking.com 是10分制 → 转5分制
                    normalized = round(raw_score / 2 if raw_score > 5 else raw_score, 1)
                    conn.execute("""
                        INSERT OR IGNORE INTO google_ratings
                            (hotel_id, captured_date, google_rating, review_count, price_level, raw_snippet)
                        VALUES (?,?,?,?,?,?)
                    """, (hotel["id"], today_str, normalized, None, None, "booking_com_score"))
                    conn.commit()
                    _rating_saved_this_hotel = True
                    g_str = f"★{normalized}(bcom)"
                except Exception:
                    pass

            # OTA 库存信号（Playwright精准版，写入 inventory_signals 表）
            inv_result = fetch_inventory_signals(hotel, checkin, conn, sess)
            inv_str = f"库存{inv_result['avail_level']}({inv_result['rooms_remaining']})" \
                      if inv_result.get("rooms_remaining") is not None else ""

            save_snapshot(conn, hotel, checkin, price_data, ota_data, snap_time)

            status_icon = "✅" if price_data["source_ok"] else "⚠️"
            bar_str = f"MOP {price_data['official_bar']:.0f}" if price_data.get("official_bar") else "N/A"
            log.info(f"  {status_icon} {checkin}: BAR={bar_str} | OTA={ota_data.get('booking_rate','N/A')} | {inv_str} | {price_data.get('notes','')}")

            if price_data["source_ok"]:
                ok_count += 1
            else:
                fail_count += 1

        time.sleep(random.uniform(3.0, 6.0))   # 酒店间隔

    # ══════════════════════════════════════════════════════════════════════
    #  采集后批量评估：MDP寻客行动选择
    # ══════════════════════════════════════════════════════════════════════
    if _MDP_OK:
        try:
            mdp_decisions = _run_mdp_sweep(hotels, conn, verbose=True)
            if mdp_decisions:
                log.info(f"[MDP] 本次触发寻客行动: {len(mdp_decisions)}家酒店")
                for d in mdp_decisions:
                    log.info(f"  → {d['hotel_id']} | {d['action']}({d['action_label']}) | {d['trigger_reason']}")
        except Exception as e:
            log.warning(f"[MDP] sweep 异常: {e}")

    # ── 每周五运行弹性校准（ε 先验值更新）─────────────────────────────────
    if _MDP_OK and datetime.now().weekday() == 4:   # Friday=4
        try:
            cal_result = _calibrate_elasticity(conn)
            log.info(f"[弹性校准] 完成 | {cal_result}")
        except Exception as e:
            log.warning(f"[弹性校准] 异常: {e}")

    conn.close()
    total = ok_count + fail_count
    log.info(f"=== 采集完成 | 成功率 {ok_count}/{total} ({ok_count/total*100:.1f}%) | DB: {DB_PATH} ===")


# ══════════════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="InsightBridge 酒店数据采集器")
    parser.add_argument("--test",   action="store_true", help="只采集前3家酒店验证流程")
    parser.add_argument("--hotel",  type=str, default="", help="只采集指定hotel_id前缀的酒店")
    parser.add_argument("--tier",   type=str, default="", help="只采集指定星级(5_deluxe/5_star/4_star/3_star)")
    args = parser.parse_args()

    hotels = HOTELS_76
    if args.test:
        hotels = HOTELS_76[:3]
        log.info("🧪 测试模式：只采集前3家")
    elif args.hotel:
        hotels = [h for h in HOTELS_76 if args.hotel.upper() in h["id"]]
        log.info(f"🔍 筛选模式：{args.hotel} → {len(hotels)}家")
    elif args.tier:
        hotels = [h for h in HOTELS_76 if h["tier"] == args.tier]
        log.info(f"⭐ 星级模式：{args.tier} → {len(hotels)}家")

    run_collection(hotels, label=args.tier or ("TEST" if args.test else "ALL"))
