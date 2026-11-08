HUNT TRACKER
============
Live stats for Hunt: Showdown 1896: an overlay over the game (press End), or a window you can keep
on a second monitor. It fills itself in while you play. You don't have to type anything.

STARTING IT
- If you ticked "Open automatically whenever Hunt starts" during setup, just launch Hunt.
  "Hunt Tracker is running" shows at the bottom of the screen for a moment.
- Otherwise use the "Hunt Tracker" shortcut in the Start menu (or on your desktop).
- Its icon sits by the clock (maybe under the ^ arrow). Right-click it for Overlay / Windowed /
  Open / Quit.

OVERLAY (the default)
- Press End in Hunt: the game dims and the tracker's panels show on top. End, Esc or a click on the
  dimmed game goes back to Hunt. While it's open your clicks stay in the overlay, not the game.
- Drag a panel to move it; it stays where you put it. Click names, stats and headings like in the
  window. The bar along the top shows or hides panels, opens the Leaderboard, REPORT AN ISSUE sends
  a note to the developer, and OVERLAY switches to a normal window.
- Prefer a window on a second monitor? Click OVERLAY (or right-click the icon > Windowed). It
  remembers your choice.

NEW IN 1.0.20
- Hunters no longer lose their hunt count (and the tracker's own kill count) once their matches
  are older than the last 100. A hunter with no tracked hunts shows just its name instead of
  "(0 hunts)".
- Most kills in one hunt and Career kills keep counting matches older than the last 100.

NEW IN 1.0.19
- A weapon's skin name is no longer read as part of the killer's weapon on the death screen.

NEW IN 1.0.18
- Your rank no longer drops when a digit is misread; it only goes down after you prestige.
- Short fragments of Discord names are no longer used to filter players, so real players are no
  longer hidden by them.
- Slightly misread hunter names are matched to the hunter you've been playing instead of being
  thrown away.
- Diagnostics reports include your K/D.

NEW IN 1.0.17
- The death screen's Damage History tab is no longer read as a killer.
- Screen text such as headings and score lines is no longer saved as players, and ones saved
  earlier are removed.
- Weapon names on the death screen no longer pick up stray tags or letters.
- Your hunter is now read from the end-of-match summary after you die.
- Career K/D is worked out from your kills and deaths when the Statistics page doesn't show it.

NEW IN 1.0.16
- The number row under player badges is no longer saved as a player.
- A player's name now shows the spelling read most often, so one misread no longer replaces it.
- Misread spellings of your hunter's name are merged into the right hunter.

NEW IN 1.0.15
- The tracker now opens a minute after Hunt starts instead of straight away, so it doesn't start
  while the game's anti-cheat is loading.
- Player and Discord names are no longer taken as your hunter.
- Your own loading card no longer shows up as another player.
- Discord's in-game overlay is no longer read as part of the game.
- The tray icon comes back if Windows drops it (for example when the taskbar restarts).
- KILLED BY shows how often you've met that killer.
- Player Summary uses the extra space in a wide window.
- "Send diagnostics now" in the tray menu, and more detail in diagnostics reports to help track
  down misreads.

NEW IN 1.0.14
- Prestige and rank update within a few seconds of opening the squad stats or Equipment page, and
  are also read from the lobby banner, killer plates and Bloodline Progress.
- The Equipment page is no longer read as a squad, and the hunter row is no longer read as player
  names.
- Players with all-zero stats stay on the loading cards.
- A loading-screen tip is no longer read as a death screen.
- A killer's hunter, weapon and rank are now what most frames of the death screen agree on.
- Player Summary's "matches read" no longer stops at 100.
- The weapon list in the overlay no longer opens in the top-left corner.
- Rank bar to 100 on the Career page, and teammates' and killers' prestige / rank shown with the
  game's medal and skull icons.

NEW IN 1.0.13
- The overlay (above), the icon by the clock, and the "running" note when Hunt starts.
- Most kills in one hunt; Career kills and deaths keep counting between visits to Statistics.
- Players whose names show as boxes are kept instead of dropped.

NEW IN 1.0.11
- LEADERBOARD at the top: share your Career stats (it asks first; change it any time) and see
  how you rank. Click a player for their full card.
- Kills by hunter: the hunter you ready up with gets that match's kills, and the kills row
  follows the hunter you're playing. Your card's "<Hunter> Kills" number takes over when shown.
- Deaths to AI (Meathead, Hive...) are counted; click a player in Player Summary for their record.
- Faster screen reading, and screens are kept as text instead of pictures.

WHAT IT SHOWS
Left side
- What you're doing (menus / searching / in a hunt). In a hunt: "mission ends in". It's an
  estimate (~) until you hold Tab to open the map, then it matches the game's timer.
- Your MMR stars (read from Team Details after a match) and total kills.
- Hunts and hours for today and the last 7 days, and lifetime totals from Hunt's logs.
- CAREER (toggle at the top of the window): your Statistics, read whenever you open your Statistics page in
  Hunt's menu. Hover your K/D there once so it can read your exact kills / deaths / assists.

Right side: Player Summary
- Your squad and their stats, read from the loading cards when a match is found.
- Who killed you this match, and how often they've killed you overall.
- Anyone you've met before shows "(encountered X times)" and their MMR from last time.
- After a match, open Team Details on the summary and scroll down: that saves everyone's MMR.

GOOD TO KNOW
- It only reads the screen while Hunt is showing (and not while the overlay is open); it never
  touches the game.
- The first time it sees your loading cards it learns your name from the middle card (yours).
- The toggles along the top of the window switch: Career, Player summary, Hunt log, Leaderboard,
  Overlay.
- Resize it by dragging its edges (or maximize it); everything scales to fit.
- Your data is kept in %LOCALAPPDATA%\HuntTracker (paste that into the Explorer address bar).
- Diagnostics: about once an hour it sends the developer a report (setup, your Career stats, error
  logs, what it recently read from Hunt's screens) to help fix problems. Right-click the icon by the clock and
  untick "Send diagnostics" to stop.
- It updates itself: when a new version is out, it downloads it as it opens and restarts
  a few seconds later (the version number is at the top right). Your data is kept.
- Windows may warn that the app is from an unknown publisher the first time: click
  "More info" > "Run anyway". It isn't signed, that's all.
