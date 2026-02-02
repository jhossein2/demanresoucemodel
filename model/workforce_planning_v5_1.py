"""
================================================================================
WORKFORCE PLANNING SYSTEM - VERSION 3
================================================================================

Fixes:
1. Show ALL 4 buckets per month (not just worst case)
2. Add shift cap - max FTE deployable per shift
3. Add occupancy metrics:
   - Agent Occupancy: How busy are agents? (Erlangs / Agents)
   - Capacity Utilization: How much of max capacity used? (Deployed / Max)
4. SLA based on DEPLOYED agents (capped by shift), not theoretical max

================================================================================
"""

# %% ============================================================================
# SECTION 1: IMPORTS AND CONFIGURATION
# ==============================================================================

import math
import calendar
import pandas as pd

# ------------------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------------------

CONFIG = {
    # File settings
    'forecast_file': 'CalVolForecastMonthly.xlsx',
    'service': 'POL',
    
    # ----------------------------------------------------------------------
    # FORECAST ADJUSTMENT
    # Scale forecast up/down for sensitivity analysis
    # ----------------------------------------------------------------------
    'forecast_scale': 1.00,       # 1.00 = baseline, 1.05 = +5%, 0.95 = -5%
    
    # Service parameters
    'aht_seconds': 240,
    'sla_target': 0.80,
    'sla_time': 5.0,
    
    # Workforce parameters
    'initial_fte': 200,
    'monthly_attrition_rate': 0.02,
    'shrinkage': 0.52,
    
    # Constraints
    'max_occupancy': 0.85,        # Burnout threshold
    'min_agents_floor': 12,       # Union minimum
    
    # ----------------------------------------------------------------------
    # SHIFT CONSTRAINTS (12-hour shifts)
    # Day shift and night shift are separate pools - staff can't work both
    # Total of day + night must not exceed total FTE available
    # ----------------------------------------------------------------------
    'max_fte_day_shift': 55,      # Max FTE rostered on day shift (7am-7pm)
    'max_fte_night_shift': 35,    # Max FTE rostered on night shift (7pm-7am)
                                   # Total: 55 + 35 = 90 (must be <= available FTE)
    
    # ----------------------------------------------------------------------
    # RECRUITMENT PIPELINE
    # ----------------------------------------------------------------------
    # Recruitment batches: list of (start_month_offset, trainees_hired)
    # start_month_offset = months from simulation start (0 = first month)
    # Example: [(0, 50), (6, 30)] = hire 50 in month 1, hire 30 in month 7
    'recruitment_batches': [
        (0, 50),    # Hire 50 trainees at simulation start (Jan 2026)
        (12, 40),   # Hire 40 trainees in month 13 (Jan 2027)
        (24, 30),   # Hire 30 trainees in month 25 (Jan 2028)
    ],
    
    'training_duration_months': 12,   # How long until trainees are deployable
    'training_success_rate': 0.70,    # 70% complete training successfully
    
    # ----------------------------------------------------------------------
    # DAY GROUPS
    # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
    # ----------------------------------------------------------------------
    'offpeak_days': [0, 1, 2, 3],  # Mon-Thu
    'peak_days': [4, 5, 6],        # Fri-Sun
    
    # ----------------------------------------------------------------------
    # VOLUME DISTRIBUTION (must sum to 1.0)
    # ----------------------------------------------------------------------
    'volume_share': {
        'offpeak_days_offpeak_hours': 0.10,
        'offpeak_days_peak_hours': 0.35,
        'peak_days_offpeak_hours': 0.10,
        'peak_days_peak_hours': 0.45,
    },
    
    # ----------------------------------------------------------------------
    # SLIDING PEAK HOURS
    # Format: (start_hour, duration_hours)
    # This handles wrap-around properly (e.g., 11pm-2am)
    # ----------------------------------------------------------------------
    'peak_hours_window': {
        'offpeak_days': (7, 15),    # Mon-Thu: 7am start, 15 hours duration → 7am-10pm
        'peak_days': (11, 15),      # Fri-Sun: 11am start, 15 hours duration → 11am-2am (wraps)
    },
    
    # Net hours per FTE per month
    'net_hours_per_fte': 78.0,
}

# Validate
total_share = sum(CONFIG['volume_share'].values())
assert abs(total_share - 1.0) < 0.001, f"Volume shares must sum to 1.0, got {total_share}"

# Note: Config display moved after helper functions are defined


# %% ============================================================================
# SECTION 2: ERLANG-C FUNCTIONS
# ==============================================================================

def erlang_intensity(calls_per_hour, aht_seconds):
    """Calculate Erlangs = (calls × AHT) / 3600"""
    if calls_per_hour <= 0 or aht_seconds <= 0:
        return 0.0
    return (calls_per_hour * aht_seconds) / 3600.0


def erlang_prob_wait(erlangs, agents):
    """Probability of waiting (Erlang-C)"""
    if agents <= 0 or erlangs <= 0:
        return 0.0
    if agents <= erlangs:
        return 1.0
    
    occupancy = erlangs / agents
    a_n = 1.0
    sum_a_k = 0.0
    
    for k in range(agents, -1, -1):
        a_k = a_n * k / erlangs if erlangs > 0 else 0
        sum_a_k += a_k
        a_n = a_k
    
    if sum_a_k == 0:
        return 0.0
    
    prob_wait = 1.0 / (1.0 + (1.0 - occupancy) * sum_a_k)
    return max(0.0, min(1.0, prob_wait))


def erlang_service_level(erlangs, agents, target_time, aht):
    """Service Level = % answered within target time"""
    if agents <= 0:
        return 0.0
    if erlangs <= 0:
        return 1.0
    if agents <= erlangs:
        return 0.0
    
    prob_wait = erlang_prob_wait(erlangs, agents)
    if prob_wait <= 0:
        return 1.0
    
    sl = 1.0 - (prob_wait * math.exp(-(agents - erlangs) * target_time / aht))
    return max(0.0, min(1.0, sl))


def erlang_agents_required(erlangs, sla_target, target_time, aht):
    """FORWARD: Agents needed to meet SLA"""
    if erlangs <= 0:
        return 1
    
    agents = max(1, int(math.ceil(erlangs)) + 1)
    for _ in range(500):
        sl = erlang_service_level(erlangs, agents, target_time, aht)
        if sl >= sla_target:
            return agents
        agents += 1
    return agents


def erlang_achievable_sla(erlangs, agents, target_time, aht):
    """REVERSE: SLA achievable with given agents"""
    return erlang_service_level(erlangs, agents, target_time, aht)


# %% ============================================================================
# SECTION 3: LOAD FORECAST DATA
# ==============================================================================

def load_forecast(file_path, service):
    """Load forecast from Excel"""
    df = pd.read_excel(file_path)
    df.columns = df.columns.str.strip().str.lower()
    df = df[df['service'].str.upper() == service.upper()]
    
    forecasts = []
    for _, row in df.iterrows():
        date_val = row['date']
        if hasattr(date_val, 'year'):
            year, month = date_val.year, date_val.month
        else:
            parts = str(date_val).split('-')
            year, month = int(parts[0]), int(parts[1])
        
        forecasts.append({
            'year': year,
            'month': month,
            'volume_base': int(row['forecast']),  # Original forecast
            'volume': int(row['forecast'])         # Will be scaled
        })
    
    return forecasts


def apply_forecast_scale(forecasts, scale):
    """Apply scaling factor to forecast volumes"""
    for f in forecasts:
        f['volume'] = int(f['volume_base'] * scale)
    return forecasts


print("\nLoading forecast...")
forecasts = load_forecast(CONFIG['forecast_file'], CONFIG['service'])
forecasts = apply_forecast_scale(forecasts, CONFIG['forecast_scale'])
print(f"Loaded {len(forecasts)} months")
print(f"Forecast scale: {CONFIG['forecast_scale']:.0%}")


# %% ============================================================================
# SECTION 4: HELPER FUNCTIONS
# ==============================================================================

def count_days_by_group(year, month, config):
    """Count off-peak days (Mon-Thu) and peak days (Fri-Sun)"""
    days_in_month = calendar.monthrange(year, month)[1]
    
    offpeak_count = 0
    peak_count = 0
    
    for day in range(1, days_in_month + 1):
        dow = calendar.weekday(year, month, day)
        if dow in config['offpeak_days']:
            offpeak_count += 1
        elif dow in config['peak_days']:
            peak_count += 1
    
    return offpeak_count, peak_count


def get_peak_window(day_group, config):
    """
    Get peak window details for a day group.
    
    Config format: (start_hour, duration_hours)
    
    Returns dict with:
        - start_hour: 0-23
        - duration: hours of peak
        - end_hour: 0-23 (handles wrap-around)
        - peak_hours: list of actual hour indices [0-23]
        - offpeak_hours: list of actual hour indices [0-23]
        - peak_count: number of peak hours
        - offpeak_count: number of off-peak hours
        - peak_day_shift_hours: peak hours that fall in day shift (7am-7pm)
        - peak_night_shift_hours: peak hours that fall in night shift (7pm-7am)
    
    Example:
        (11, 15) → 11am to 2am (wraps around midnight)
        peak_hours = [11,12,13,14,15,16,17,18,19,20,21,22,23,0,1]
    """
    start_hour, duration = config['peak_hours_window'][day_group]
    
    # Generate list of peak hour indices (handles wrap-around)
    peak_hours = []
    for i in range(duration):
        hour = (start_hour + i) % 24
        peak_hours.append(hour)
    
    # Off-peak hours are everything else
    all_hours = set(range(24))
    offpeak_hours = sorted(list(all_hours - set(peak_hours)))
    
    # End hour (for display)
    end_hour = (start_hour + duration) % 24
    
    # Classify hours by shift
    # Day shift: 7am-7pm (hours 7-18 inclusive)
    # Night shift: 7pm-7am (hours 19-23 and 0-6)
    day_shift_hours = set(range(7, 19))  # 7, 8, ..., 18
    night_shift_hours = set(range(0, 7)) | set(range(19, 24))  # 0-6 and 19-23
    
    peak_day_shift_hours = [h for h in peak_hours if h in day_shift_hours]
    peak_night_shift_hours = [h for h in peak_hours if h in night_shift_hours]
    offpeak_day_shift_hours = [h for h in offpeak_hours if h in day_shift_hours]
    offpeak_night_shift_hours = [h for h in offpeak_hours if h in night_shift_hours]
    
    return {
        'start_hour': start_hour,
        'duration': duration,
        'end_hour': end_hour,
        'peak_hours': peak_hours,
        'offpeak_hours': offpeak_hours,
        'peak_count': len(peak_hours),
        'offpeak_count': len(offpeak_hours),
        'peak_day_shift_hours': peak_day_shift_hours,
        'peak_night_shift_hours': peak_night_shift_hours,
        'peak_day_shift_count': len(peak_day_shift_hours),
        'peak_night_shift_count': len(peak_night_shift_hours),
        'offpeak_day_shift_hours': offpeak_day_shift_hours,
        'offpeak_night_shift_hours': offpeak_night_shift_hours,
        'offpeak_day_shift_count': len(offpeak_day_shift_hours),
        'offpeak_night_shift_count': len(offpeak_night_shift_hours),
    }


def get_hours_split(day_group, config):
    """
    Get peak and off-peak hour counts for a day group.
    (Wrapper for backward compatibility)
    """
    window = get_peak_window(day_group, config)
    return window['peak_count'], window['offpeak_count']


def format_time_range(start_hour, end_hour):
    """Format hour range for display (e.g., '7am-10pm' or '11am-2am')"""
    def fmt(h):
        if h == 0: return '12am'
        if h == 12: return '12pm'
        if h < 12: return f'{h}am'
        return f'{h-12}pm'
    return f"{fmt(start_hour)}-{fmt(end_hour)}"


def fte_to_agents(fte, shrinkage):
    """Convert FTE to available agents (apply shrinkage)"""
    return fte * (1 - shrinkage)


def agents_to_fte(agents, shrinkage):
    """Convert agents to FTE required"""
    return agents / (1 - shrinkage) if (1 - shrinkage) > 0 else agents


# %% ============================================================================
# SECTION 4B: RECRUITMENT PIPELINE
# ==============================================================================

class RecruitmentPipeline:
    """
    Manages the recruitment pipeline with training delay and attrition.
    
    Trainees enter the pipeline and graduate after training_duration months.
    Only success_rate % of trainees complete training successfully.
    
    Example:
        - Hire 100 trainees in Jan 2026
        - Training duration: 12 months
        - Success rate: 70%
        - Result: 70 FTE added in Jan 2027
    """
    
    def __init__(self, config):
        self.training_duration = config['training_duration_months']
        self.success_rate = config['training_success_rate']
        self.batches = config['recruitment_batches']
        
        # Pipeline: dict of {graduation_month_offset: graduating_fte}
        self.pipeline = {}
        
        # Initialize pipeline from recruitment batches
        for start_month, trainees in self.batches:
            graduation_month = start_month + self.training_duration
            graduating_fte = int(trainees * self.success_rate)
            
            if graduation_month in self.pipeline:
                self.pipeline[graduation_month] += graduating_fte
            else:
                self.pipeline[graduation_month] = graduating_fte
    
    def get_graduates(self, month_offset):
        """Get FTE graduating in a specific month (0-indexed from simulation start)"""
        return self.pipeline.get(month_offset, 0)
    
    def get_trainees_in_pipeline(self, month_offset):
        """Get total trainees currently in training at a given month"""
        total = 0
        for start_month, trainees in self.batches:
            graduation_month = start_month + self.training_duration
            # Trainee is in pipeline if: start_month <= month_offset < graduation_month
            if start_month <= month_offset < graduation_month:
                total += trainees
        return total
    
    def get_pipeline_summary(self):
        """Get summary of all recruitment batches"""
        summary = []
        for start_month, trainees in self.batches:
            graduation_month = start_month + self.training_duration
            graduating_fte = int(trainees * self.success_rate)
            summary.append({
                'start_month': start_month,
                'trainees_hired': trainees,
                'graduation_month': graduation_month,
                'graduating_fte': graduating_fte,
                'attrition_during_training': trainees - graduating_fte,
            })
        return summary


def print_recruitment_summary(pipeline):
    """Print recruitment pipeline summary"""
    print(f"\n=== RECRUITMENT PIPELINE ===")
    print(f"Training Duration: {pipeline.training_duration} months")
    print(f"Success Rate: {pipeline.success_rate:.0%}")
    print(f"\nBatches:")
    print(f"  {'Start Month':<12} {'Trainees':<10} {'Grad Month':<12} {'Grad FTE':<10} {'Attrition':<10}")
    print(f"  {'-'*54}")
    
    total_hired = 0
    total_graduating = 0
    
    for batch in pipeline.get_pipeline_summary():
        print(f"  Month {batch['start_month']:<6} {batch['trainees_hired']:<10} Month {batch['graduation_month']:<6} {batch['graduating_fte']:<10} {batch['attrition_during_training']:<10}")
        total_hired += batch['trainees_hired']
        total_graduating += batch['graduating_fte']
    
    print(f"  {'-'*54}")
    print(f"  {'TOTAL':<12} {total_hired:<10} {'':<12} {total_graduating:<10} {total_hired - total_graduating:<10}")


# ------------------------------------------------------------------------------
# Display Configuration (after helper functions are defined)
# ------------------------------------------------------------------------------
print("=== CONFIGURATION ===")
print(f"Service: {CONFIG['service']}")
print(f"Initial FTE: {CONFIG['initial_fte']}")
print(f"Shrinkage: {CONFIG['shrinkage']:.0%}")
print(f"Max Occupancy (burnout threshold): {CONFIG['max_occupancy']:.0%}")

print(f"\nShift Constraints:")
print(f"  Day Shift (7am-7pm):   Max {CONFIG['max_fte_day_shift']} FTE")
print(f"  Night Shift (7pm-7am): Max {CONFIG['max_fte_night_shift']} FTE")
print(f"  Total Shift Capacity:  {CONFIG['max_fte_day_shift'] + CONFIG['max_fte_night_shift']} FTE")

print(f"\nPeak Hours Windows:")
for day_group in ['offpeak_days', 'peak_days']:
    window = get_peak_window(day_group, CONFIG)
    time_str = format_time_range(window['start_hour'], window['end_hour'])
    label = "Mon-Thu" if day_group == 'offpeak_days' else "Fri-Sun"
    print(f"  {label}: {time_str} ({window['peak_count']} peak hrs, {window['offpeak_count']} off-peak hrs)")
    print(f"    Peak hours: {window['peak_hours']}")
    print(f"    - Day shift: {window['peak_day_shift_count']} hrs {window['peak_day_shift_hours']}")
    print(f"    - Night shift: {window['peak_night_shift_count']} hrs {window['peak_night_shift_hours']}")


# %% ============================================================================
# SECTION 5: CALCULATE ALL BUCKETS FOR A MONTH
# ==============================================================================

def calculate_month_buckets(year, month, volume, current_fte, config, graduates=0, trainees_in_pipeline=0):
    """
    Calculate demand and supply for ALL 4 buckets in a month.
    
    Args:
        year, month: Time period
        volume: Monthly call volume
        current_fte: FTE at start of month
        config: Configuration dict
        graduates: FTE graduating from training this month (added to pool)
        trainees_in_pipeline: Trainees currently in training (for reporting)
    
    Returns list of 4 bucket results (one per bucket).
    """
    days_in_month = calendar.monthrange(year, month)[1]
    offpeak_days_count, peak_days_count = count_days_by_group(year, month, config)
    
    # Hours per day group
    offpeak_days_peak_hrs, offpeak_days_offpeak_hrs = get_hours_split('offpeak_days', config)
    peak_days_peak_hrs, peak_days_offpeak_hrs = get_hours_split('peak_days', config)
    
    # Define bucket metadata
    bucket_defs = {
        'Mon-Thu Off-Peak': {
            'key': 'offpeak_days_offpeak_hours',
            'days': offpeak_days_count,
            'hours_per_day': offpeak_days_offpeak_hrs,
        },
        'Mon-Thu Peak': {
            'key': 'offpeak_days_peak_hours',
            'days': offpeak_days_count,
            'hours_per_day': offpeak_days_peak_hrs,
        },
        'Fri-Sun Off-Peak': {
            'key': 'peak_days_offpeak_hours',
            'days': peak_days_count,
            'hours_per_day': peak_days_offpeak_hrs,
        },
        'Fri-Sun Peak': {
            'key': 'peak_days_peak_hours',
            'days': peak_days_count,
            'hours_per_day': peak_days_peak_hrs,
        },
    }
    
    # PHASE 3: Resource Inventory (The Leaky Bucket)
    # Step 3.1: Stock Decay - Apply attrition
    fte_after_attrition = current_fte * (1 - config['monthly_attrition_rate'])
    
    # Step 3.2: Add graduates from recruitment pipeline
    fte_after_graduates = fte_after_attrition + graduates
    
    # Step 3.3: Apply shift caps (day and night are separate pools)
    # Ensure total shift allocation doesn't exceed available FTE
    max_fte_day = config['max_fte_day_shift']
    max_fte_night = config['max_fte_night_shift']
    total_shift_cap = max_fte_day + max_fte_night
    
    # If we don't have enough FTE, scale down proportionally
    if fte_after_graduates < total_shift_cap:
        scale_factor = fte_after_graduates / total_shift_cap
        effective_fte_day = max_fte_day * scale_factor
        effective_fte_night = max_fte_night * scale_factor
    else:
        effective_fte_day = max_fte_day
        effective_fte_night = max_fte_night
    
    results = []
    
    for bucket_name, bucket_def in bucket_defs.items():
        key = bucket_def['key']
        days = bucket_def['days']
        hours_per_day = bucket_def['hours_per_day']
        total_hours = days * hours_per_day  # Hours this bucket needs coverage
        
        # Determine which day group this bucket belongs to
        is_peak_days = 'Fri-Sun' in bucket_name
        is_peak_hours = 'Peak' in bucket_name and 'Off-Peak' not in bucket_name
        
        day_group = 'peak_days' if is_peak_days else 'offpeak_days'
        window = get_peak_window(day_group, config)
        
        # Get hours breakdown for this bucket by shift
        if is_peak_hours:
            day_shift_hours = window['peak_day_shift_count']
            night_shift_hours = window['peak_night_shift_count']
        else:
            day_shift_hours = window['offpeak_day_shift_count']
            night_shift_hours = window['offpeak_night_shift_count']
        
        # Volume for this bucket
        bucket_volume = volume * config['volume_share'][key]
        
        # Step 3.4: Capacity Conversion using Roster Multiplier
        # Calculate separately for day and night portions
        day_hours_month = days * day_shift_hours
        night_hours_month = days * night_shift_hours
        
        # Roster multiplier for each shift portion
        roster_mult_day = day_hours_month / config['net_hours_per_fte'] if config['net_hours_per_fte'] > 0 else 0
        roster_mult_night = night_hours_month / config['net_hours_per_fte'] if config['net_hours_per_fte'] > 0 else 0
        
        # Available chairs from each shift
        chairs_from_day = int(effective_fte_day / roster_mult_day) if roster_mult_day > 0 else 0
        chairs_from_night = int(effective_fte_night / roster_mult_night) if roster_mult_night > 0 else 0
        
        # The binding constraint is the MINIMUM of day and night capacity
        # (we need to staff both shifts to cover the bucket)
        if day_hours_month > 0 and night_hours_month > 0:
            n_available = min(chairs_from_day, chairs_from_night)
            binding_shift = 'day' if chairs_from_day <= chairs_from_night else 'night'
        elif day_hours_month > 0:
            n_available = chairs_from_day
            binding_shift = 'day'
        elif night_hours_month > 0:
            n_available = chairs_from_night
            binding_shift = 'night'
        else:
            n_available = 0
            binding_shift = 'none'
        
        # Roster multipliers per shift (for FTE calculations)
        roster_mult_day = day_hours_month / config['net_hours_per_fte'] if config['net_hours_per_fte'] > 0 else 0
        roster_mult_night = night_hours_month / config['net_hours_per_fte'] if config['net_hours_per_fte'] > 0 else 0
        roster_multiplier_total = roster_mult_day + roster_mult_night  # For display
        
        # Calls per hour and Erlangs
        if total_hours > 0:
            calls_per_hour = bucket_volume / total_hours
        else:
            calls_per_hour = 0
        erlangs = erlang_intensity(calls_per_hour, config['aht_seconds'])
        
        # PHASE 2: Logistics Sizing (The Requirement)
        # Constraint A: Erlang C for SLA
        agents_for_sla = erlang_agents_required(
            erlangs, config['sla_target'], config['sla_time'], config['aht_seconds']
        )
        # Constraint B: Occupancy cap
        agents_for_occupancy = math.ceil(erlangs / config['max_occupancy']) if erlangs > 0 else 0
        # Constraint C: Floor (HARD constraint)
        agents_floor = config['min_agents_floor']
        
        # N_required = max of all constraints
        n_required = max(agents_for_sla, agents_for_occupancy, agents_floor)
        
        # PHASE 4: Performance Audit (The Result)
        
        # Floor is HARD constraint - we MUST deploy at least floor
        # Only if n_available >= floor, we can meet it
        # If n_available < floor, we still deploy floor (overtime/borrowed staff) but flag breach
        floor_breach = n_available < agents_floor  # Can't meet floor with available capacity
        
        # N_deployed: enforce floor as minimum, cap at available (but at least floor)
        if n_available >= agents_floor:
            # Normal case: deploy what's needed, capped by available
            n_deployed = min(n_required, n_available)
        else:
            # Breach case: we MUST deploy floor (hard constraint), even if over capacity
            # This represents forced overtime, borrowed staff, or compliance requirement
            n_deployed = agents_floor
        
        # 4.1 Reverse Erlang: Achievable SLA with deployed agents
        achievable_sla = erlang_achievable_sla(
            erlangs, n_deployed, config['sla_time'], config['aht_seconds']
        )
        
        # Agent Occupancy: How busy are deployed agents? (Erlangs / Deployed)
        if n_deployed > 0:
            agent_occupancy = erlangs / n_deployed
        else:
            agent_occupancy = 1.0
        
        # Capacity Utilization: What % of available capacity are we using?
        # Can exceed 100% if we're forced to deploy floor above available
        if n_available > 0:
            capacity_utilization = n_deployed / n_available
        else:
            capacity_utilization = 1.0
        
        # Chair gap (positive = surplus, negative = deficit)
        chair_gap = n_available - n_required
        
        # 4.2 Recruitment Sizing: FTE needed to meet demand
        # FTE required is SUM of day pool + night pool requirements (not combined roster mult)
        # Each pool needs to independently provide n_required chairs during its hours
        fte_required_day = n_required * roster_mult_day if day_hours_month > 0 else 0
        fte_required_night = n_required * roster_mult_night if night_hours_month > 0 else 0
        fte_required = fte_required_day + fte_required_night
        
        # 4.3 Breach detection
        sla_breach = achievable_sla < config['sla_target']
        occupancy_breach = agent_occupancy > config['max_occupancy']
        # floor_breach already calculated above
        
        # Zone classification (floor breach is most severe - it's a compliance failure)
        if floor_breach:
            zone = "Floor Breach"
        elif sla_breach:
            zone = "SLA Breach"
        elif occupancy_breach:
            zone = "Burnout Risk"
        elif capacity_utilization > 0.95:
            zone = "Critical"
        elif capacity_utilization > 0.85:
            zone = "Caution"
        elif capacity_utilization > 0.75:
            zone = "Elevated"
        else:
            zone = "OK"
        
        results.append({
            'year': year,
            'month': month,
            'month_name': calendar.month_abbr[month],
            'bucket': bucket_name,
            'volume': int(bucket_volume),
            'total_hours': total_hours,
            'day_shift_hours': day_hours_month,
            'night_shift_hours': night_hours_month,
            'calls_per_hour': calls_per_hour,
            'erlangs': erlangs,
            'agents_floor': agents_floor,
            'n_required': n_required,
            'n_available': n_available,
            'chairs_from_day': chairs_from_day,
            'chairs_from_night': chairs_from_night,
            'binding_shift': binding_shift,
            'n_deployed': n_deployed,
            'chair_gap': chair_gap,
            'roster_mult_day': roster_mult_day,
            'roster_mult_night': roster_mult_night,
            'achievable_sla': achievable_sla,
            'agent_occupancy': agent_occupancy,
            'capacity_utilization': capacity_utilization,
            'fte_day_shift': effective_fte_day,
            'fte_night_shift': effective_fte_night,
            'fte_start_of_month': current_fte,
            'fte_after_attrition': fte_after_attrition,
            'fte_graduates': graduates,
            'fte_end_of_month': fte_after_graduates,
            'trainees_in_pipeline': trainees_in_pipeline,
            'fte_required_day': fte_required_day,
            'fte_required_night': fte_required_night,
            'fte_required': fte_required,
            'sla_breach': sla_breach,
            'occupancy_breach': occupancy_breach,
            'floor_breach': floor_breach,
            'zone': zone,
        })
    
    return results, fte_after_graduates


# %% ============================================================================
# SECTION 6: TEST SINGLE MONTH
# ==============================================================================

print("\n=== TEST: JANUARY 2026 (All 4 Buckets) ===")
test_results, _ = calculate_month_buckets(2026, 1, 149247, 200, CONFIG)

print(f"\n{'Bucket':<20} {'Volume':>8} {'Hours':>6} {'Calls/hr':>9} {'Erlang':>7} {'N_req':>6} {'N_avail':>7} {'N_dep':>6} {'Gap':>5} {'FTE_req':>8} {'SLA':>6} {'Occ':>6} {'Zone':<12}")
print("-" * 130)

for r in test_results:
    print(f"{r['bucket']:<20} {r['volume']:>8,} {r['total_hours']:>6} {r['calls_per_hour']:>9.1f} {r['erlangs']:>7.1f} {r['n_required']:>6} {r['n_available']:>7} {r['n_deployed']:>6} {r['chair_gap']:>+5} {r['fte_required']:>8.1f} {r['achievable_sla']:>5.0%} {r['agent_occupancy']:>5.0%} {r['zone']:<12}")


# %% ============================================================================
# SECTION 7: RUN FULL SIMULATION
# ==============================================================================

def run_simulation(forecasts, config):
    """
    Run simulation with recruitment pipeline.
    
    Returns ALL bucket rows for ALL months.
    """
    all_results = []
    current_fte = float(config['initial_fte'])
    
    # Initialize recruitment pipeline
    pipeline = RecruitmentPipeline(config)
    
    for month_offset, f in enumerate(forecasts):
        # Get graduates for this month
        graduates = pipeline.get_graduates(month_offset)
        trainees_in_pipeline = pipeline.get_trainees_in_pipeline(month_offset)
        
        month_results, current_fte = calculate_month_buckets(
            f['year'], f['month'], f['volume'], current_fte, config,
            graduates=graduates,
            trainees_in_pipeline=trainees_in_pipeline
        )
        all_results.extend(month_results)
    
    return all_results, pipeline


# Print recruitment pipeline summary
recruitment_pipeline = RecruitmentPipeline(CONFIG)
print_recruitment_summary(recruitment_pipeline)

print("\n=== RUNNING FULL SIMULATION ===")
all_results, pipeline = run_simulation(forecasts, CONFIG)
print(f"Generated {len(all_results)} bucket rows ({len(forecasts)} months × 4 buckets)")


# %% ============================================================================
# SECTION 8: CONVERT TO DATAFRAME
# ==============================================================================

df = pd.DataFrame(all_results)

# Reorder columns
columns = [
    'year', 'month', 'month_name', 'bucket',
    'volume', 'total_hours', 'day_shift_hours', 'night_shift_hours',
    'calls_per_hour', 'erlangs',
    'agents_floor', 'n_required', 'n_available', 
    'chairs_from_day', 'chairs_from_night', 'binding_shift',
    'n_deployed', 'chair_gap',
    'roster_mult_day', 'roster_mult_night',
    'achievable_sla', 'agent_occupancy', 'capacity_utilization',
    'fte_day_shift', 'fte_night_shift',
    'fte_start_of_month', 'fte_after_attrition', 'fte_graduates', 'fte_end_of_month',
    'trainees_in_pipeline', 'fte_required_day', 'fte_required_night', 'fte_required',
    'floor_breach', 'sla_breach', 'occupancy_breach', 'zone'
]
df = df[columns]

print("\n=== DATAFRAME PREVIEW (First 12 rows = 3 months × 4 buckets) ===")
pd.set_option('display.width', 250)
pd.set_option('display.max_columns', 35)
print(df.head(12).to_string(index=False))


# %% ============================================================================
# SECTION 9: SUMMARY ANALYSIS
# ==============================================================================

print("\n" + "=" * 100)
print("SUMMARY ANALYSIS")
print("=" * 100)

# Floor Breaches (Union/EBA compliance failure - most severe)
floor_breaches = df[df['floor_breach'] == True]
print(f"\n🚨 FLOOR BREACHES (Union min not met): {len(floor_breaches)} bucket-months")

if len(floor_breaches) > 0:
    first_floor = floor_breaches.iloc[0]
    print(f"   First breach: {first_floor['month_name']} {first_floor['year']} - {first_floor['bucket']}")
    print(f"   Floor required: {first_floor['agents_floor']} | Available capacity: {first_floor['n_available']}")
    print(f"   Deployed: {first_floor['n_deployed']} (floor enforced despite insufficient capacity)")
    print(f"   ⚠️  This is a COMPLIANCE FAILURE - requires overtime/borrowed staff!")

# SLA Breaches
sla_breaches = df[df['sla_breach'] == True]
print(f"\n📉 SLA BREACHES: {len(sla_breaches)} bucket-months")

if len(sla_breaches) > 0:
    first_breach = sla_breaches.iloc[0]
    print(f"   First breach: {first_breach['month_name']} {first_breach['year']} - {first_breach['bucket']}")
    print(f"   Achievable SLA: {first_breach['achievable_sla']:.1%}")
    print(f"   Required: {first_breach['n_required']} | Available: {first_breach['n_available']} | Deployed: {first_breach['n_deployed']}")

# Burnout Risk (Occupancy > 85%)
burnout_risk = df[df['occupancy_breach'] == True]
print(f"\n🔥 BURNOUT RISK (Occ > 85%): {len(burnout_risk)} bucket-months")

if len(burnout_risk) > 0:
    first_burnout = burnout_risk.iloc[0]
    print(f"   First occurrence: {first_burnout['month_name']} {first_burnout['year']} - {first_burnout['bucket']}")
    print(f"   Occupancy: {first_burnout['agent_occupancy']:.1%}")

# High Capacity Utilization (>90%)
high_capacity = df[df['capacity_utilization'] > 0.90]
print(f"\n⚠️  HIGH CAPACITY (>90%): {len(high_capacity)} bucket-months")

# Zone distribution
print("\n📊 ZONE DISTRIBUTION:")
zone_counts = df['zone'].value_counts()
for zone, count in zone_counts.items():
    pct = count / len(df) * 100
    print(f"   {zone}: {count} ({pct:.1f}%)")

# Peak bucket analysis (usually Fri-Sun Peak)
print("\n📈 WORST BUCKET ANALYSIS (Fri-Sun Peak):")
peak_bucket = df[df['bucket'] == 'Fri-Sun Peak']
print(f"   Avg Erlangs: {peak_bucket['erlangs'].mean():.1f}")
print(f"   Avg Occupancy: {peak_bucket['agent_occupancy'].mean():.1%}")
print(f"   Avg Capacity Util: {peak_bucket['capacity_utilization'].mean():.1%}")
print(f"   SLA Breaches: {peak_bucket['sla_breach'].sum()}")


# %% ============================================================================
# SECTION 10: MONTHLY SUMMARY (Aggregate 4 buckets into 1 row per month)
# ==============================================================================

def create_monthly_summary(df):
    """
    Aggregate 4 bucket rows into 1 monthly insight row.
    
    Logic:
    - Volume: Sum of all buckets
    - Erlangs: Max (worst case bucket)
    - N_required: Max across buckets
    - N_available: Min across buckets (binding constraint)
    - SLA: Min (worst performing bucket)
    - Occupancy: Max (highest stress bucket)
    - Zone: Worst zone across buckets
    - Binding bucket: Which bucket is constraining
    """
    
    # Zone severity order (for finding worst)
    zone_severity = {
        'OK': 0, 'Elevated': 1, 'Caution': 2, 'Critical': 3,
        'Burnout Risk': 4, 'SLA Breach': 5, 'Floor Breach': 6
    }
    
    monthly_rows = []
    
    for (year, month), group in df.groupby(['year', 'month']):
        # Find worst bucket (highest erlangs)
        worst_idx = group['erlangs'].idxmax()
        worst_bucket = group.loc[worst_idx]
        
        # Find binding bucket (lowest n_available)
        binding_idx = group['n_available'].idxmin()
        binding_bucket = group.loc[binding_idx]
        
        # Find worst zone
        worst_zone_row = group.loc[group['zone'].map(zone_severity).idxmax()]
        
        monthly_rows.append({
            'year': year,
            'month': month,
            'month_name': worst_bucket['month_name'],
            
            # Volume
            'volume_total': group['volume'].sum(),
            
            # Demand (worst bucket)
            'worst_bucket': worst_bucket['bucket'],
            'peak_erlangs': worst_bucket['erlangs'],
            'peak_calls_per_hour': worst_bucket['calls_per_hour'],
            
            # Supply
            'n_required': group['n_required'].max(),
            'n_available': group['n_available'].min(),
            'n_deployed': group['n_deployed'].min(),
            'binding_bucket': binding_bucket['bucket'],
            'binding_shift': binding_bucket['binding_shift'],
            
            # Performance (worst case)
            'min_sla': group['achievable_sla'].min(),
            'max_occupancy': group['agent_occupancy'].max(),
            'max_capacity_util': group['capacity_utilization'].max(),
            
            # FTE
            'fte_end_of_month': worst_bucket['fte_end_of_month'],
            'fte_graduates': worst_bucket['fte_graduates'],
            'trainees_in_pipeline': worst_bucket['trainees_in_pipeline'],
            'fte_required_peak': group['fte_required'].max(),
            
            # Breaches
            'any_floor_breach': group['floor_breach'].any(),
            'any_sla_breach': group['sla_breach'].any(),
            'any_occupancy_breach': group['occupancy_breach'].any(),
            'buckets_breaching_sla': group['sla_breach'].sum(),
            
            # Zone (worst across all buckets)
            'zone': worst_zone_row['zone'],
        })
    
    return pd.DataFrame(monthly_rows)


# Create monthly summary
monthly_df = create_monthly_summary(df)

print("\n" + "=" * 120)
print("MONTHLY SUMMARY (1 row per month - aggregated from 4 buckets)")
print("=" * 120)

# Display key columns
display_cols = [
    'year', 'month_name', 'volume_total', 'worst_bucket', 'peak_erlangs',
    'n_required', 'n_available', 'min_sla', 'max_occupancy', 
    'fte_end_of_month', 'fte_graduates', 'zone'
]
print(monthly_df[display_cols].head(24).to_string(index=False))


# %% ============================================================================
# SECTION 11: SENSITIVITY ANALYSIS
# ==============================================================================

def run_sensitivity_analysis(base_forecasts, config, scales=[0.95, 1.00, 1.05, 1.10]):
    """
    Run simulation at different forecast scales to show sensitivity.
    """
    import copy
    results = []
    
    for scale in scales:
        # Deep copy forecasts to avoid mutation
        test_forecasts = copy.deepcopy(base_forecasts)
        
        # Apply scale to volume
        for f in test_forecasts:
            f['volume'] = int(f['volume_base'] * scale)
        
        # Run simulation
        sim_results, _ = run_simulation(test_forecasts, config)
        sim_df = pd.DataFrame(sim_results)
        
        # Aggregate metrics
        sla_breaches = sim_df['sla_breach'].sum()
        floor_breaches = sim_df['floor_breach'].sum()
        peak_data = sim_df[sim_df['bucket'] == 'Fri-Sun Peak']
        avg_occupancy = peak_data['agent_occupancy'].mean()
        avg_sla = peak_data['achievable_sla'].mean()
        
        # Find first breach
        breach_rows = sim_df[sim_df['sla_breach']]
        if len(breach_rows) > 0:
            first_breach = breach_rows.iloc[0]
            first_breach_month = f"{first_breach['month_name']} {first_breach['year']}"
        else:
            first_breach_month = "None"
        
        results.append({
            'scale': f"{scale:.0%}",
            'volume_change': f"{(scale-1)*100:+.0f}%",
            'avg_sla_peak': f"{avg_sla:.1%}",
            'avg_occupancy_peak': f"{avg_occupancy:.1%}",
            'sla_breaches': sla_breaches,
            'floor_breaches': floor_breaches,
            'first_breach': first_breach_month,
        })
    
    return pd.DataFrame(results)


print("\n" + "=" * 80)
print("SENSITIVITY ANALYSIS (Impact of Volume Changes)")
print("=" * 80)

# Reset forecast to base before sensitivity
for f in forecasts:
    f['volume'] = f['volume_base']

sensitivity_df = run_sensitivity_analysis(forecasts, CONFIG, scales=[0.90, 0.95, 1.00, 1.05, 1.10, 1.15])
print(sensitivity_df.to_string(index=False))


# %% ============================================================================
# SECTION 12: EXPORT
# ==============================================================================

# Save detailed bucket-level results
output_file = 'workforce_planning_buckets.csv'
df.to_csv(output_file, index=False)
print(f"\n✅ Bucket-level results exported to: {output_file}")

# Save monthly summary
monthly_output = 'workforce_planning_monthly.csv'
monthly_df.to_csv(monthly_output, index=False)
print(f"✅ Monthly summary exported to: {monthly_output}")
