import random
from dataclasses import dataclass

# ==========================================
# CONFIG
# ==========================================

STARTING_BANKROLL = 700
TOTAL_GAMES = 20000
MIN_BET = 1
MAX_BET = 100

# Randomly choose from every game mode equally.
# You can change the weights if desired.

@dataclass
class GameMode:
    name: str
    house_win_rate: float
    payout: float

GAME_MODES = [
    GameMode("FT1 Fair", 0.5000, 0.85),
    GameMode("FT3 Fair", 0.5000, 0.85),
    GameMode("FT5 Fair", 0.5000, 0.85),

    GameMode("FT3 House Wins Ties", 0.6047, 1.10),
    GameMode("FT5 House Wins Ties", 0.6363, 1.30),

    GameMode("FT3 House Wins 7s", 0.7905, 2.00),
    GameMode("FT5 House Wins 7s", 0.8556, 3.00),

    # Approximate values
    GameMode("FT3 House Wins 7s + Ties", 0.8500, 3.00),
    GameMode("FT5 House Wins 7s + Ties", 0.9100, 3.50),
]

# ==========================================
# STATS
# ==========================================

bankroll = STARTING_BANKROLL
peak_bankroll = bankroll
largest_drawdown = 0

wins = 0
losses = 0
busts = 0

total_profit = 0
total_bets = 0

mode_stats = {}

largest_win = 0
largest_loss = 0

current_win_streak = 0
current_loss_streak = 0

best_win_streak = 0
best_loss_streak = 0

milestones = [100, 500, 1000, 2500, 5000]

print("=" * 60)
print("Starting Monte Carlo Simulation")
print("=" * 60)

for game in range(1, TOTAL_GAMES + 1):

    mode = random.choice(GAME_MODES)
    bet = random.randint(MIN_BET, MAX_BET)

    total_bets += bet

    # House must be able to pay if it loses
    payout_amount = bet * mode.payout

    if bankroll < payout_amount:
        busts += 1
        print(f"\nHOUSE BUST ON GAME {game}")
        print(f"Bankroll: ${bankroll:.2f}")
        print(f"Needed:    ${payout_amount:.2f}")
        break

    if random.random() < mode.house_win_rate:
        bankroll += bet
        total_profit += bet
        wins += 1

        current_win_streak += 1
        current_loss_streak = 0

        best_win_streak = max(best_win_streak, current_win_streak)

        largest_win = max(largest_win, bet)

        result = "WIN"

    else:
        bankroll -= payout_amount
        total_profit -= payout_amount
        losses += 1

        current_loss_streak += 1
        current_win_streak = 0

        best_loss_streak = max(best_loss_streak, current_loss_streak)

        largest_loss = max(largest_loss, payout_amount)

        result = "LOSS"

    if bankroll > peak_bankroll:
        peak_bankroll = bankroll

    drawdown = peak_bankroll - bankroll

    if drawdown > largest_drawdown:
        largest_drawdown = drawdown

    if mode.name not in mode_stats:
        mode_stats[mode.name] = {
            "games": 0,
            "wins": 0,
            "losses": 0,
            "profit": 0,
        }

    mode_stats[mode.name]["games"] += 1

    if result == "WIN":
        mode_stats[mode.name]["wins"] += 1
        mode_stats[mode.name]["profit"] += bet
    else:
        mode_stats[mode.name]["losses"] += 1
        mode_stats[mode.name]["profit"] -= payout_amount

    if game in milestones:
        print(f"\n{'='*60}")
        print(f"AFTER {game} GAMES")
        print("="*60)

        print(f"Bankroll: ${bankroll:.2f}")
        print(f"Profit:   ${bankroll-STARTING_BANKROLL:.2f}")
        print(f"ROI:      {(bankroll-STARTING_BANKROLL)/STARTING_BANKROLL*100:.2f}%")
        print(f"W/L:      {wins}/{losses}")

print("\n")
print("="*60)
print("FINAL RESULTS")
print("="*60)

print(f"Games Played:       {wins+losses}")
print(f"House Wins:         {wins}")
print(f"House Losses:       {losses}")
print(f"Win Rate:           {wins/(wins+losses)*100:.2f}%")

print()

print(f"Starting Bankroll:  ${STARTING_BANKROLL:.2f}")
print(f"Ending Bankroll:    ${bankroll:.2f}")
print(f"Net Profit:         ${bankroll-STARTING_BANKROLL:.2f}")

print()

print(f"Total Challenger Bets: ${total_bets}")
print(f"Expected Profit/Game: ${(bankroll-STARTING_BANKROLL)/(wins+losses):.2f}")

print()

print(f"Largest Single Win:    ${largest_win}")
print(f"Largest Single Loss:   ${largest_loss:.2f}")

print()

print(f"Best Win Streak:    {best_win_streak}")
print(f"Best Loss Streak:   {best_loss_streak}")

print()

print(f"Peak Bankroll:      ${peak_bankroll:.2f}")
print(f"Largest Drawdown:   ${largest_drawdown:.2f}")

print()

print(f"Busts:              {busts}")

print("\n")
print("="*60)
print("GAME MODE BREAKDOWN")
print("="*60)

for mode, stats in sorted(mode_stats.items()):
    games = stats["games"]

    if games == 0:
        continue

    wr = stats["wins"]/games*100

    print(f"\n{mode}")
    print("-"*40)
    print(f"Games:      {games}")
    print(f"Win Rate:   {wr:.2f}%")
    print(f"Profit:     ${stats['profit']:.2f}")