"""Central configuration. Every value can be overridden from .env (never commit .env)."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      env_ignore_empty=True)

    # --- Mode -------------------------------------------------------------
    # DEMO  = paper broker or MT5 demo account. LIVE needs LIVE_TRADING_ENABLED=true
    # AND a separate confirmation from the dashboard (see /api/mode).
    trading_mode: str = "DEMO"
    live_trading_enabled: bool = False
    # Account number the owner has explicitly authorised for automated trading. Persisting it here
    # avoids re-confirming after every restart; any OTHER account still needs the dashboard phrase.
    live_confirmed_account: str | None = None
    # execution: MANUAL | ASSISTED | SEMI_AUTO | AUTO
    execution_mode: str = "AUTO"

    # --- Data source ------------------------------------------------------
    # SIMULATION = synthetic prices (clearly labelled, never real market data)
    # MT5        = MetaTrader 5 terminal on this Windows machine
    data_source: str = "SIMULATION"
    symbols: str = "XAUUSD,BTCUSD,EURUSD,GBPUSD"
    paper_balance: float = 100.0  # starting balance for the simulator only

    # --- MT5 (fill on the VPS only) ----------------------------------------
    mt5_login: int | None = None
    mt5_password: str | None = None
    mt5_server: str | None = None
    mt5_path: str | None = None
    exn_api_key: str | None = None
    exn_private_key: str | None = None
    exn_discovery_base_url: str | None = None
    exn_account_id: str | None = None
    mt5_magic: int = 515151
    # Exness account types may suffix symbols (e.g. "m" -> XAUUSDm). Check Market Watch.
    symbol_suffix: str = ""
    max_deviation_points: int = 30

    # --- Strategy profile: AGGRESSIVE_SCALP (default for the $100 demo test) --
    # Timeframe roles: dominant -> confirm -> setup (structure, SL) -> entry (trigger)
    tf_dominant: str = "H1"
    tf_confirm: str = "M15"
    tf_setup: str = "M5"
    tf_entry: str = "M1"
    auto_min_score: float = 65.0  # AUTO executes MODERATE (65+) and STRONG (80+)
    tp1_r: float = 1.5  # TP1 at 1.5R -> close 50% + break-even
    tp2_r: float = 2.5
    tp3_r: float = 3.5  # fallback when no liquidity target
    sl_min_atr: float = 1.0
    sl_max_atr: float = 2.5
    trailing_atr: float = 1.5  # after TP1, trail SL by 1.5 ATR of the setup timeframe
    max_hold_minutes: int = 240  # close stale scalps
    close_before_weekend: bool = True
    # Symbols that trade 7 days a week (crypto). Others are skipped while FX/gold is closed.
    always_open_symbols: str = "BTCUSD"
    keep_awake_weekends: bool = True  # keep the Render instance awake on weekends for crypto

    # --- Risk hard limits -------------------------------------------------
    # Aggressive but never martingale: risk is a fixed % of current equity,
    # it is never raised to recover losses.
    risk_per_trade_pct: float = 3.0
    max_risk_per_trade_pct: float = 10.0  # absolute ceiling
    daily_max_loss_pct: float = 25.0
    weekly_max_loss_pct: float = 30.0
    max_drawdown_pct: float = 40.0
    max_open_trades: int = 5
    max_trades_per_symbol: int = 1
    max_consecutive_losses: int = 8
    max_spread_to_sl_ratio: float = 0.20
    news_block_minutes: int = 15
    strategy_mode: str = "HYBRID"  # HYBRID = research-playbook agent; SCALP = fixed-$ micro trades; SMC = legacy
    # --- hybrid agent (regime -> primary signal -> meta-label p -> fractional Kelly; staged exits) ---
    hybrid_tf: str = "M15"
    hybrid_sl_atr: float = 1.5  # structural stop distance in ATR
    hybrid_tp1_r: float = 1.5  # close 40% + break-even
    hybrid_tp2_r: float = 3.0  # close another 40%
    hybrid_tp3_r: float = 5.0  # broker-side safety TP; runner is trailed before this
    hybrid_trail_atr: float = 2.5  # chandelier trail for the runner
    hybrid_horizon_bars: int = 48  # vertical barrier / time stop (48 x M15 = 12h)
    hybrid_risk_max_pct: float = 1.5  # ATR-normalised risk cap per trade
    hybrid_minlot_max_risk_pct: float = 10.0  # allow broker min lot only if it risks <= this
    hybrid_max_open: int = 4
    evidence_gate: bool = False  # True = block symbols with negative walk-forward expectancy
    kelly_fraction: float = 0.25  # quarter Kelly
    meta_p_min: float = 0.42  # meta-model probability threshold
    meta_min_events: int = 60  # min labelled events before the model may trade
    meta_train_bars: int = 3000
    meta_retrain_hours: int = 12
    scalp_risk_pct: float = 2.5  # stop per trade = this % of equity (grows with the account)
    scalp_tp_mult: float = 1.0  # take profit = stop $ x this
    scalp_tp_usd: float = 1.5  # legacy/min floor
    scalp_sl_usd: float = 1.5  # legacy/min floor
    scalp_max_minutes: int = 20  # close a scalp that hasn't resolved
    scalp_cooldown_min: int = 2  # same-symbol pause after an entry
    scalp_loss_cooldown_min: int = 10  # same-symbol pause after a loss
    scalp_max_open: int = 4
    votes_min_agree: int = 12  # of 20 indicators on the setup TF must agree with the direction
    profit_lock_start_usd: float = 5.0  # once the day is up this much...
    profit_lock_keep_pct: float = 50.0  # ...keep at least this % of the day's best gain; below it, bot entries pause till tomorrow
    profit_lock_pause_min: int = 45  # pause after a give-back, then resume
    tg_max_per_symbol: int = 6  # Telegram trades are separate: one per entry zone + reinforcements
    tg_lots: float = 0.01  # fixed lot per Telegram entry (per zone)
    tg_minlot_max_risk_pct: float = 15.0  # refuse a Telegram entry only if 0.01 lot would risk more than this
    tg_context_min: int = 20  # levels posted within N min attach to the analyst's last side-only order  # Telegram trades are separate: entries + reinforcements per symbol
    boost_risk_pct: float = 5.0  # STRONG setup while the day is in profit and no loss streak -> risk up to this
    reentry_cooldown_min: int = 15  # no new entry on the same symbol for N minutes after an entry
    loss_cooldown_min: int = 30  # ...and N minutes after a losing exit on that symbol

    # --- Telegram analyst channels -----------------------------------------
    tg_api_id: str | None = None
    tg_api_hash: str | None = None
    tg_session_key: str | None = None
    tg_channels: str = "الصفقة الصباحية,ABNGAZA"
    tg_mode: str = "follow"  # track | smart | follow
    tg_min_closed: int = 10
    tg_min_winrate: float = 50.0
    tg_agree_score: float = 50.0
    tg_risk_pct: float = 2.0

    # --- Security ---------------------------------------------------------
    dashboard_token: str = "change-me"
    webhook_secret: str = "change-me-too"
    webhook_max_age_s: int = 30

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]


settings = Settings()
