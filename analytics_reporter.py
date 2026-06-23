# analytics_reporter.py
# This script will generate a daily report on face recognition performance.

import json
import os
import re
import shutil
from datetime import datetime, date, timedelta
import requests
import numpy as np
from collections import defaultdict

def load_config():
    """Loads the configuration from config.json."""
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: config.json not found at {config_path}")
        return None
    except json.JSONDecodeError:
        print(f"Error: Could not decode config.json. Please check its format.")
        return None

def get_staff_master_list(config):
    """
    Fetches the master list of staff from the API.
    """
    if not config or "Server" not in config or "API_url" not in config["Server"]:
        print("Error: API URL not found in config.json.")
        return None, None

    api_url = config["Server"]["API_url"]
    staffs_endpoint = f"{api_url}/staffs/"
    
    print(f"Fetching staff master list from {staffs_endpoint}...")
    try:
        response = requests.get(staffs_endpoint, timeout=10)
        response.raise_for_status()
        staff_data = response.json()
        staff_id_to_name = {item["staff_id"]: item["username"] for item in staff_data}
        valid_staff_ids = set(staff_id_to_name.keys())
        print(f"Successfully fetched {len(valid_staff_ids)} staff members.")
        return staff_id_to_name, valid_staff_ids
    except Exception as e:
        print(f"Error during API call: {e}")
        return None, None

def parse_recognition_log(target_date: date):
    """Parses the faceLog file for a specific date and extracts recognition events."""
    log_dir = os.path.join(os.path.dirname(__file__), "log")
    log_file_path = os.path.join(log_dir, f"faceLog.{target_date.strftime('%Y-%m-%d')}.log")
    
    if target_date == date.today() and not os.path.exists(log_file_path):
        log_file_path = os.path.join(log_dir, "faceLog")

    print(f"Parsing recognition log: {log_file_path}...")
    if not os.path.exists(log_file_path):
        print(f"Recognition log not found for date {target_date}.")
        return []

    log_pattern = re.compile(
        r".*?\[辨識事件\].*?ID: (.*?)\).*?"
        r"信賴度: (.*?)%.*?"
        r"Z-Score: (.*?),"
        r".*?Width: (.*?) px"
        r".*?\[評級: (.*?)(\s.*?)?\]"
    )
    parsed_events = []
    try:
        with open(log_file_path, 'r', encoding='utf-8') as f:
            for line in f:
                match = log_pattern.search(line)
                if match:
                    try:
                        staff_id, confidence, z_score, width, rating = match.groups()[:5]
                        parsed_events.append({
                            "staff_id": staff_id.strip(),
                            "confidence": float(confidence.strip()),
                            "z_score": float(z_score.strip()),
                            "width": int(width.strip()),
                            "rating": rating.strip().split(" ")[0]
                        })
                    except (ValueError, IndexError):
                        continue
    except Exception as e:
        print(f"Error reading recognition log file {log_file_path}: {e}")

    print(f"Found {len(parsed_events)} recognition events.")
    return parsed_events

def parse_performance_log(target_date: date):
    """Parses the perfLog file for a specific date and extracts performance JSON data."""
    log_dir = os.path.join(os.path.dirname(__file__), "log")
    log_file_path = os.path.join(log_dir, f"perfLog.{target_date.strftime('%Y-%m-%d')}.log")

    if target_date == date.today() and not os.path.exists(log_file_path):
        log_file_path = os.path.join(log_dir, "perfLog")

    print(f"Parsing performance log: {log_file_path}...")
    if not os.path.exists(log_file_path):
        print(f"Performance log not found for date {target_date}.")
        return []
    
    performance_events = []
    try:
        with open(log_file_path, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    if line.strip():
                        performance_events.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue # Skip malformed lines
    except Exception as e:
        print(f"Error reading performance log file {log_file_path}: {e}")
    
    print(f"Found {len(performance_events)} performance events.")
    return performance_events

def parse_width_stats(target_date: date):
    """Parses faceLog to aggregate hourly width distribution statistics by camera."""
    log_dir = os.path.join(os.path.dirname(__file__), "log")
    log_file_path = os.path.join(log_dir, f"faceLog.{target_date.strftime('%Y-%m-%d')}.log")
    
    if target_date == date.today() and not os.path.exists(log_file_path):
        log_file_path = os.path.join(log_dir, "faceLog")

    if not os.path.exists(log_file_path):
        return {}

    # 結構化存儲: {"入口": {"80-89": 5, ...}, "出口": {...}}
    width_stats = defaultdict(lambda: defaultdict(int))
    # 支援新格式 "[統計] [入口] ..." 與舊格式 "[統計] ..." (歸類為 未知)
    stats_pattern = re.compile(r"\[統計\]\s*(?:\[(.*?)\])?.*?分佈:\s*(.*)")
    
    try:
        with open(log_file_path, 'r', encoding='utf-8') as f:
            for line in f:
                match = stats_pattern.search(line)
                if match:
                    cam_name = match.group(1) or "未知"
                    content = match.group(2) # e.g., "80-89: 5, 90-99: 10"
                    parts = content.split(', ')
                    for part in parts:
                        try:
                            if ': ' in part:
                                k, v = part.split(': ')
                                width_stats[cam_name][k] += int(v)
                        except ValueError:
                            continue
    except Exception as e:
        print(f"Error parsing width stats: {e}")
        
    return {k: dict(v) for k, v in width_stats.items()}

def parse_potential_misses(target_date: date):
    """Counts the number of 'potential miss' events logged."""
    log_dir = os.path.join(os.path.dirname(__file__), "log")
    log_file_path = os.path.join(log_dir, f"faceLog.{target_date.strftime('%Y-%m-%d')}.log")
    
    if target_date == date.today() and not os.path.exists(log_file_path):
        log_file_path = os.path.join(log_dir, "faceLog")

    if not os.path.exists(log_file_path):
        return 0

    count = 0
    try:
        with open(log_file_path, 'r', encoding='utf-8') as f:
            for line in f:
                if "[潛在失敗]" in line:
                    count += 1
    except Exception:
        pass
    return count

def parse_network_issues(target_date: date):
    """
    Parses faceLog to find network diagnosis logs and aggregates them by 5-minute intervals.
    Returns a sorted list of intervals with failure details.
    """
    log_dir = os.path.join(os.path.dirname(__file__), "log")
    log_file_path = os.path.join(log_dir, f"faceLog.{target_date.strftime('%Y-%m-%d')}.log")
    
    if target_date == date.today() and not os.path.exists(log_file_path):
        log_file_path = os.path.join(log_dir, "faceLog")

    if not os.path.exists(log_file_path):
        return {}

    # Structure: { "HH:MM - HH:MM": {"count": 0, "external_fail": 0, "server_fail": 0} }
    network_stats = defaultdict(lambda: {"count": 0, "external_fail": 0, "server_fail": 0})
    
    # Regex to capture timestamp and diagnosis message
    # Log format: [YYYY-MM-DD HH:MM:SS,mmm] ... 上傳失敗網路診斷: ...
    log_pattern = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\].*?上傳失敗網路診斷:\s*(.*)")
    
    try:
        with open(log_file_path, 'r', encoding='utf-8') as f:
            for line in f:
                match = log_pattern.search(line)
                if match:
                    ts_str = match.group(1)
                    diagnosis_msg = match.group(2)
                    
                    try:
                        # Parse timestamp
                        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
                        
                        # Calculate 5-minute bucket
                        minute_bucket = (dt.minute // 5) * 5
                        start_time = dt.replace(minute=minute_bucket, second=0, microsecond=0)
                        end_time = start_time + timedelta(minutes=5)
                        interval_key = f"{start_time.strftime('%H:%M')} ~ {end_time.strftime('%H:%M')}"
                        
                        # Update stats
                        stats = network_stats[interval_key]
                        stats["count"] += 1
                        
                        if "外部網路(8.8.8.8): 無法連線" in diagnosis_msg:
                            stats["external_fail"] += 1
                        if "伺服器" in diagnosis_msg and "無法連線" in diagnosis_msg:
                             # Check if it's specifically server failure (not just implied by external fail)
                             # Usually if external fails, server fails too, but we track count anyway
                             stats["server_fail"] += 1
                             
                    except ValueError:
                        continue
                        
    except Exception as e:
        print(f"Error parsing network logs: {e}")
        
    return dict(sorted(network_stats.items()))


def calculate_statistics(log_events, staff_id_to_name, valid_staff_ids):
    """
    Calculates overall and per-person statistics from parsed log events.
    """
    print("Calculating statistics...")
    overall_stats = defaultdict(int)
    overall_stats['raw_reliable_events'] = [] # Store raw events for later analysis (e.g. width stats)
    
    per_person_stats = defaultdict(lambda: {
        'name': '',
        'reliable_recognitions': [],
        'ambiguous_count': 0,
        'low_confidence_count': 0,
        'total_appearances': 0,
    })

    for event in log_events:
        overall_stats['total_events'] += 1
        rating = event['rating']
        staff_id = event['staff_id']
        
        per_person_stats[staff_id]['total_appearances'] += 1
        per_person_stats[staff_id]['name'] = staff_id_to_name.get(staff_id, f"未知ID ({staff_id})")

        if rating == "可靠":
            if staff_id in valid_staff_ids:
                overall_stats['true_positive_count'] += 1
            else:
                overall_stats['false_positive_count'] += 1
            
            per_person_stats[staff_id]['reliable_recognitions'].append(event)
            overall_stats['raw_reliable_events'].append(event) # Add to raw list
        elif rating == "模糊":
            overall_stats['ambiguous_count'] += 1
            per_person_stats[staff_id]['ambiguous_count'] += 1
        else:
            overall_stats['low_confidence_count'] += 1
            per_person_stats[staff_id]['low_confidence_count'] += 1
            
    final_person_stats = {}
    for staff_id, data in per_person_stats.items():
        reliable_events = data['reliable_recognitions']
        stats = {
            'name': data['name'],
            'is_valid_staff': staff_id in valid_staff_ids,
            'reliable_count': len(reliable_events),
            'ambiguous_count': data['ambiguous_count'],
            'low_confidence_count': data['low_confidence_count'],
            'total_appearances': data['total_appearances'],
        }
        if reliable_events:
            confidences = [e['confidence'] for e in reliable_events]
            z_scores = [e['z_score'] for e in reliable_events]
            stats.update({
                'avg_confidence': np.mean(confidences),
                'max_confidence': np.max(confidences),
                'min_confidence': np.min(confidences),
                'avg_z_score': np.mean(z_scores),
            })
        final_person_stats[staff_id] = stats

    return dict(overall_stats), final_person_stats

def calculate_performance_statistics(perf_events):
    """
    Calculates performance statistics from parsed performance events.
    """
    print("Calculating performance statistics...")
    if not perf_events:
        return None

    detection_times = [e['duration_sec'] for e in perf_events if e['type'] == 'detection']
    comparison_times = [e['duration_sec'] for e in perf_events if e['type'] == 'comparison']
    
    per_person_comparison = defaultdict(list)
    for e in perf_events:
        if e['type'] == 'comparison' and 'person_id' in e:
            per_person_comparison[e['person_id']].append(e['duration_sec'])

    stats = {
        'detection': {},
        'comparison': {},
        'per_person_comparison_avg': {}
    }

    if detection_times:
        stats['detection'] = {
            'avg': np.mean(detection_times),
            'min': np.min(detection_times),
            'max': np.max(detection_times),
            'count': len(detection_times)
        }
    
    if comparison_times:
        stats['comparison'] = {
            'avg': np.mean(comparison_times),
            'min': np.min(comparison_times),
            'max': np.max(comparison_times),
            'count': len(comparison_times)
        }

    if per_person_comparison:
        stats['per_person_comparison_avg'] = {
            person_id: np.mean(times) for person_id, times in per_person_comparison.items()
        }

    return stats

def write_text_report(overall_stats, per_person_stats, perf_stats, width_stats, potential_miss_count, network_stats, report_path, min_face_settings):
    """Formats the report and writes (overwrites) it to the specified text file."""
    report_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    total = overall_stats.get('total_events', 0)
    
    # Start with recognition report
    report_lines = [
        "#"*80,
        f"# Report Generated at: {report_time}",
        "#"*80,
        "\n--- 整體辨識狀況 (Overall Performance) ---\n",
    ]

    if total == 0:
        report_lines.append("No recognition events to report for today.")
    else:
        true_pos = overall_stats.get('true_positive_count', 0)
        false_pos = overall_stats.get('false_positive_count', 0)
        ambiguous = overall_stats.get('ambiguous_count', 0)
        low_conf = overall_stats.get('low_confidence_count', 0)
        false_positive_rate = (false_pos / total * 100) if total > 0 else 0
        
        report_lines.extend([
            f"總辨識事件 (Total Events): {total}",
            f"  - 我方人員可靠辨識 (True Positives): {true_pos} ({true_pos/total:.2%})",
            f"  - 誤判為陌生人 (False Positives):   {false_pos} ({false_pos/total:.2%})",
            f"  - 模糊辨識 (Ambiguous):           {ambiguous} ({ambiguous/total:.2%})",
            f"  - 低信賴度 (Low Conf):             {low_conf} ({low_conf/total:.2%})",
            f"\n真實誤判率 (False Positive Rate): {false_positive_rate:.2f}%",
            "\n* 真實誤判率 = (誤判為陌生人) / 總事件數",
        ])

    # --- 新增: 網路連線穩定度分析 ---
    report_lines.append("\n" + "-"*80)
    report_lines.append("--- 網路連線異常分析 (Network Stability Analysis) ---")
    
    if network_stats:
        report_lines.append(f"\n偵測到 {sum(s['count'] for s in network_stats.values())} 次上傳失敗事件 (已達重試上限)。")
        report_lines.append("統計區間 (5分鐘) | 失敗次數 | 異常原因分析")
        report_lines.append("-" * 60)
        
        for interval, stats in network_stats.items():
            count = stats['count']
            ext_fail = stats['external_fail']
            srv_fail = stats['server_fail']
            
            reasons = []
            if ext_fail > 0:
                reasons.append(f"外網斷線({ext_fail})")
            
            # 假設外網斷線通常也會導致伺服器連線失敗 (srv_fail >= ext_fail)
            # 我們只顯示"額外"的伺服器異常 (即外網通但伺服器不通)
            pure_srv_fail = max(0, srv_fail - ext_fail)
            if pure_srv_fail > 0:
                reasons.append(f"伺服器異常({pure_srv_fail})")
                 
            reason_str = ", ".join(reasons) if reasons else "不明原因"
            report_lines.append(f"{interval:<17} | {count:<8} | {reason_str}")
            
        report_lines.append("-" * 60)
        report_lines.append("* '外網斷線' 表示無法 Ping 到 8.8.8.8。")
        report_lines.append("* '伺服器異常' 表示外網可通但 API 無法連線。")
    else:
        report_lines.append("\n本日無上傳失敗紀錄 (網路狀況良好)。")


    # --- 新增: 潛在失敗與寬度分佈分析 ---
    report_lines.append("\n" + "-"*80)
    report_lines.append("--- 辨識門檻與寬度分析 (Threshold & Width Analysis) ---")
    
    # 顯示當前設定
    in_min = min_face_settings.get('in', 'N/A')
    out_min = min_face_settings.get('out', 'N/A')
    max_face = min_face_settings.get('max', 'N/A')
    
    report_lines.append(f"\n目前設定 (Current Settings):")
    
    # 入口設定詳情
    in_intent_str = "N/A"
    if isinstance(in_min, int):
        in_intent_min = int(in_min * 0.8)
        in_intent_str = f"{in_intent_min} ~ {in_min} px"
    report_lines.append(f"  - 入口 (In/Cam0): min_face = {in_min} px | 意圖區間 (Intent Zone) ≈ {in_intent_str}")

    # 出口設定詳情
    out_intent_str = "N/A"
    if isinstance(out_min, int):
        out_intent_min = int(out_min * 0.8)
        out_intent_str = f"{out_intent_min} ~ {out_min} px"
    report_lines.append(f"  - 出口 (Out/Cam1): min_face = {out_min} px | 意圖區間 (Intent Zone) ≈ {out_intent_str}")
    
    report_lines.append(f"  - 全域上限 (Max Face): {max_face} px (超過此值將提示後退)")

    report_lines.append(f"\n[潛在失敗偵測] (Near Misses): {potential_miss_count} 次")
    report_lines.append(f"  * 定義：人臉寬度介於該鏡頭 'min_face' 的 80%~100% 之間。")
    report_lines.append("  * 建議：若此數值過高，請考慮降低對應鏡頭的 'min_face' 設定。")

    if width_stats:
        report_lines.append(f"\n[偵測人臉寬度分佈] (含失敗/路人/所有偵測):")
        
        def sort_key(k):
            try:
                return int(k.split('-')[0])
            except: return 0

        # 取得各自門檻以便對比
        threshold_map = {
            "入口": in_min if isinstance(in_min, int) else 0,
            "出口": out_min if isinstance(out_min, int) else 0,
            "未知": 150
        }

        for cam_name, stats in sorted(width_stats.items()):
            cam_threshold = threshold_map.get(cam_name, 150)
            report_lines.append(f"\n  * {cam_name} (當前門檻: {cam_threshold} px):")
            
            sorted_stats = sorted(stats.items(), key=lambda i: sort_key(i[0]))
            for bin_range, count in sorted_stats:
                bin_start = sort_key(bin_range)
                marker = " [有效]"
                if bin_start < cam_threshold:
                    marker = " [低於目前門檻]"
                elif isinstance(max_face, int) and bin_start >= max_face:
                    marker = " [高於目前門檻]"
                    
                report_lines.append(f"    - {bin_range} px: {count} 次{marker}")
    else:
        report_lines.append("\n[偵測人臉寬度分佈]: 無數據 (尚未累積滿一小時或無人經過)")
        
    # --- 新增: 成功辨識的臉寬分佈 (Reliable Width Stats) ---
    # 統計所有 rating="可靠" 的事件其 width 分佈 (每 50px 一區間)
    reliable_widths = [e['width'] for e in overall_stats.get('raw_reliable_events', []) if 'width' in e]
    
    if reliable_widths:
        width_bins = defaultdict(int)
        for w in reliable_widths:
            bin_start = (w // 50) * 50
            bin_key = f"{bin_start}-{bin_start+49}"
            width_bins[bin_key] += 1
            
        report_lines.append(f"\n[成功辨識臉寬分佈] (Successful Recognition Widths):")
        report_lines.append(f"  * 統計僅包含 '可靠' 評級的事件。")
        
        sorted_bins = sorted(width_bins.items(), key=lambda x: int(x[0].split('-')[0]))
        for bin_range, count in sorted_bins:
            report_lines.append(f"    - {bin_range} px: {count} 次")
    else:
        report_lines.append(f"\n[成功辨識臉寬分佈]: 無可靠辨識數據。")
    # ------------------------------------

    report_lines.append("\n" + "-"*80)
    report_lines.append("--- 個別人員辨識統計 (Per-Person Statistics) ---")

    known_staff_stats = {s: d for s, d in per_person_stats.items() if d['is_valid_staff']}
    unknown_id_stats = {s: d for s, d in per_person_stats.items() if not d['is_valid_staff']}

    if known_staff_stats:
        report_lines.append("\n--- 專案內人員 (Registered Staff) ---\n")
        for sid, stats in sorted(known_staff_stats.items(), key=lambda i: i[1]['total_appearances'], reverse=True):
            report_lines.append(f"人員: {stats['name']} (ID: {sid})")
            report_lines.append(f"  - 總出現: {stats['total_appearances']} | 可靠: {stats['reliable_count']} | 模糊/低信賴度: {stats['ambiguous_count']}/{stats['low_confidence_count']}")
            if stats['reliable_count'] > 0:
                report_lines.append(f"    - 可靠辨識分數 -> 平均信賴度: {stats['avg_confidence']:.2f}%, 信賴度區間: [{stats['min_confidence']:.2f}%, {stats['max_confidence']:.2f}%], 平均 Z-Score: {stats['avg_z_score']:.2f}")

    if unknown_id_stats:
        report_lines.append("\n--- 專案外 ID (Unregistered IDs) ---\n")
        for sid, stats in sorted(unknown_id_stats.items(), key=lambda i: i[1]['total_appearances'], reverse=True):
            report_lines.append(f"ID: {sid}")
            report_lines.append(f"  - 總出現: {stats['total_appearances']} | 可靠(誤判): {stats['reliable_count']} | 模糊/低信賴度: {stats['ambiguous_count']}/{stats['low_confidence_count']}")
            if stats['reliable_count'] > 0:
                 report_lines.append(f"    - 誤判分數 -> 平均信賴度: {stats['avg_confidence']:.2f}%, 信賴度區間: [{stats['min_confidence']:.2f}%, {stats['max_confidence']:.2f}%], 平均 Z-Score: {stats['avg_z_score']:.2f}")
    
    # Add performance statistics section
    report_lines.append("\n" + "-"*80)
    report_lines.append("--- 系統效能統計 (System Performance Statistics) ---")

    if not perf_stats:
        report_lines.append("\nNo performance data found for today.")
    else:
        if 'detection' in perf_stats and perf_stats['detection']:
            d_stats = perf_stats['detection']
            report_lines.append(f"\n人臉偵測 (Face Detection) - 共 {d_stats['count']} 次:")
            report_lines.append(f"  - 平均耗時: {d_stats['avg']*1000:.2f} ms")
            report_lines.append(f"  - 最快耗時: {d_stats['min']*1000:.2f} ms")
            report_lines.append(f"  - 最慢耗時: {d_stats['max']*1000:.2f} ms")
        
        if 'comparison' in perf_stats and perf_stats['comparison']:
            c_stats = perf_stats['comparison']
            report_lines.append(f"\n人臉比對 (Face Comparison) - 共 {c_stats['count']} 次:")
            report_lines.append(f"  - 平均耗時: {c_stats['avg']*1000:.2f} ms")
            report_lines.append(f"  - 最快耗時: {c_stats['min']*1000:.2f} ms")
            report_lines.append(f"  - 最慢耗時: {c_stats['max']*1000:.2f} ms")

        if 'per_person_comparison_avg' in perf_stats and perf_stats['per_person_comparison_avg']:
            report_lines.append("\n個別人員平均比對耗時 (Per-Person Avg. Comparison Time):")
            # Get names for the IDs
            all_names = {**{s: d['name'] for s,d in known_staff_stats.items()}, 
                         **{s: f"未知ID ({s})" for s,d in unknown_id_stats.items()}}
            
            sorted_perf = sorted(perf_stats['per_person_comparison_avg'].items(), key=lambda i: i[1], reverse=True)
            for person_id, avg_time in sorted_perf:
                name = all_names.get(person_id, f"未知ID ({person_id})")
                report_lines.append(f"  - {name:<20}: {avg_time*1000:.2f} ms")

    report_lines.append("\n" + "="*80 + "\n")
    
    try:
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(report_lines))
        print(f"Successfully wrote (overwrote) text report to {report_path}")
    except Exception as e:
        print(f"Error writing text report to file {report_path}: {e}")


def save_stats_as_json(overall_stats, per_person_stats, json_path):
    """Saves the raw statistics as a JSON file, overwriting it."""
    print(f"Saving daily stats to {json_path}...")
    try:
        data_to_save = {
            "last_updated": datetime.now().isoformat(),
            "overall_stats": overall_stats,
            "per_person_stats": per_person_stats
        }
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error saving stats to JSON file {json_path}: {e}")

def generate_rolling_summary(report_dir):
    """Reads the last 7 days of JSON data and generates a summary report."""
    print("Generating 7-day rolling summary report...")
    today = date.today()
    weekly_stats_list = []

    for i in range(7):
        target_date = today - timedelta(days=i)
        json_path = os.path.join(report_dir, f"data-{target_date.strftime('%Y-%m-%d')}.json")
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    daily_data = json.load(f)
                    weekly_stats_list.append(daily_data['overall_stats'])
            except Exception as e:
                print(f"Could not process daily data file {json_path}: {e}")
    
    if not weekly_stats_list:
        print("No data available for the weekly summary.")
        return

    # Aggregate stats
    total_events = sum(s.get('total_events', 0) for s in weekly_stats_list)
    total_true_pos = sum(s.get('true_positive_count', 0) for s in weekly_stats_list)
    total_false_pos = sum(s.get('false_positive_count', 0) for s in weekly_stats_list)
    
    avg_true_pos_rate = (total_true_pos / total_events * 100) if total_events > 0 else 0
    avg_false_pos_rate = (total_false_pos / total_events * 100) if total_events > 0 else 0

    summary_lines = [
        "="*80,
        f"滾動式七日辨識成效總結報告 (7-Day Rolling Performance Summary)",
        f"報告生成時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"數據範圍: {(today - timedelta(days=6)).strftime('%Y-%m-%d')} ~ {today.strftime('%Y-%m-%d')}",
        "="*80,
        "\n--- 過去七日平均表現 ---\\n",
        f"總辨識事件 (Total Events): {total_events}",
        f"平均我方人員可靠辨識率: {avg_true_pos_rate:.2f}%",
        f"平均真實誤判率 (陌生人): {avg_false_pos_rate:.2f}%",
        "\n* 誤判率為「可靠的陌生人辨識」佔總事件的比例。",
        "="*80,
    ]
    
    summary_path = os.path.join(report_dir, "summary_7_days.txt")
    try:
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(summary_lines))
        print(f"Successfully wrote rolling summary to {summary_path}")
    except Exception as e:
        print(f"Error writing rolling summary: {e}")

def cleanup_old_files(report_dir, days_to_keep=7, today=None):
    """
    Deletes report files and image directories older than a specified number of days.
    """
    today = today or date.today()
    days_to_keep = max(1, int(days_to_keep))
    print(f"Starting cleanup process (keeping last {days_to_keep} days)...")
    cutoff_date = today - timedelta(days=days_to_keep - 1)

    # 1. Cleanup Report Files
    if os.path.isdir(report_dir):
        print(f"Cleaning up reports in {report_dir}...")
        for filename in os.listdir(report_dir):
            if filename.startswith("report-") or filename.startswith("data-"):
                try:
                    date_str = re.search(r'\d{4}-\d{2}-\d{2}', filename).group()
                    file_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                    
                    if file_date < cutoff_date:
                        file_path = os.path.join(report_dir, filename)
                        os.remove(file_path)
                        print(f"Deleted old report: {file_path}")
                except (AttributeError, ValueError):
                    continue

    # 2. Cleanup Image Directories
    # Define paths relative to the script location
    base_dir = os.path.dirname(__file__)
    img_dirs_to_clean = [
        os.path.join(base_dir, "img_log", "face"),
        os.path.join(base_dir, "img_log", "potential_miss")
    ]

    for img_dir in img_dirs_to_clean:
        if not os.path.isdir(img_dir):
            continue
            
        print(f"Cleaning up images in {img_dir}...")
        for dirname in os.listdir(img_dir):
            dir_path = os.path.join(img_dir, dirname)
            if not os.path.isdir(dir_path):
                continue
                
            # Try to parse date from directory name (supports YYYY-MM-DD and YYYY_MM_DD)
            try:
                # Replace underscore with dash to unify format for parsing
                normalized_date_str = dirname.replace('_', '-')
                dir_date = datetime.strptime(normalized_date_str, '%Y-%m-%d').date()
                
                if dir_date < cutoff_date:
                    shutil.rmtree(dir_path)
                    print(f"Deleted old image directory: {dir_path}")
            except ValueError:
                # Skip directories that don't match the date format
                continue

def main():
    """Main function to orchestrate the report generation."""
    print("Starting hourly recognition performance analysis...")

    report_dir = os.path.join(os.path.dirname(__file__), "reports")
    os.makedirs(report_dir, exist_ok=True)
    cleanup_old_files(report_dir, days_to_keep=7)
    
    config = load_config()
    if not config:
        return

    staff_id_to_name, valid_staff_ids = get_staff_master_list(config)
    if not valid_staff_ids:
        print("Could not retrieve staff master list. Aborting.")
        return

    target_date = date.today()
    recognition_events = parse_recognition_log(target_date)
    performance_events = parse_performance_log(target_date)
    
    # New: Parse width stats and potential misses
    width_stats = parse_width_stats(target_date)
    potential_miss_count = parse_potential_misses(target_date)
    network_stats = parse_network_issues(target_date)
    
    # Extract min_face/max_face settings for reporting
    min_face_settings = {'in': 'N/A', 'out': 'N/A', 'max': 'N/A'}
    if config:
        try:
            min_face_settings['in'] = config.get("inCamera", {}).get("min_face", "N/A")
            min_face_settings['out'] = config.get("outCamera", {}).get("min_face", "N/A")
            min_face_settings['max'] = config.get("max_face", "N/A")
        except: pass

    if not recognition_events and not performance_events and not width_stats and potential_miss_count == 0 and not network_stats:
        print("No log events of any type found for today. Nothing to report.")
        return
        
    overall_stats, per_person_stats = calculate_statistics(recognition_events, staff_id_to_name, valid_staff_ids)
    perf_stats = calculate_performance_statistics(performance_events)
    
    # 1. Write (overwrite) text report for the current run
    text_report_path = os.path.join(report_dir, f"report-{target_date.strftime('%Y-%m-%d')}.txt")
    write_text_report(overall_stats, per_person_stats, perf_stats, width_stats, potential_miss_count, network_stats, text_report_path, min_face_settings)

    # 2. Overwrite the JSON data file with the latest full-day stats (optional: could add perf_stats here too)
    json_data_path = os.path.join(report_dir, f"data-{target_date.strftime('%Y-%m-%d')}.json")
    save_stats_as_json(overall_stats, per_person_stats, json_data_path) # Note: perf_stats is not saved to JSON for now

    # 3. Regenerate the 7-day rolling summary
    generate_rolling_summary(report_dir)

    print("\nAnalysis complete.")


if __name__ == "__main__":
    main()
