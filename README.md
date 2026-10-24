# Smart Meter Technician Dispatch & Routing Optimizer

This project plans the daily work of the technician teams who repair
offline smart meters in Bangkok. Its goal is to get each meter fixed before its
billing reading day while driving as little as possible. Travel times come from
Dijkstra's algorithm on the real Bangkok road network, and Google OR-Tools
decides which meters each team visits, on which day and in what order.
Everything runs offline on an ordinary office PC.

This project is already in use at my workplace, so some of the content, code
and buttons are in Thai, as they are used there. I've translated them as
closely to their meaning as I can.

## Background

Smart meters send their readings automatically. When one stops communicating,
a technician team has to go and repair it. If the repair doesn't happen before
that meter's reading day, the automatic reading for the month is lost and has
to be handled some other way.

A few hundred meters are usually waiting, and four teams can fix only 20 to 30
of them a day. Picking the closest reading dates sends teams back and forth
across the city, and picking the meters nearest the office keeps losing
readings on meters that happen to be far away.

With the team-days we have, I wanted to work out which meters we should
repair, on which day, by which team and along which route, so that we lose as
few readings as possible and, among the plans that lose equally few, drive the
least.

The choice of jobs and the routes have to be worked out together. If you pick
the urgent jobs first and route them afterwards, you never get to ask whether
one urgent meter on the far edge of the city is worth half a team's day.

## Workflow

```
workforce.xlsx
  Pending Job, Working Day, Branch Location, Working History
        │
        ▼
1. Read the jobs, each team's capacity per day and the start/end depots
2. Work out each meter's reading date and sort it into a deadline class
        │
        ▼
3. Travel time and distance between every pair of points
   (Dijkstra's algorithm on the Bangkok road graph, cached on disk)
        │
        ▼
4. One Google OR-Tools routing solve over all days and teams of the plan
        │
        ▼
5. Hand the routes to teams so the long-run workload evens out
6. Re-check the plan from scratch (audit); stop if anything is wrong
        │
        ▼
7. Write day 1 to the Daily Route sheet, append Working History,
   draw the route map
```

The plan covers several working days at once, but only the first day is
written for the teams. The next morning the plan is made again from the latest
data. Repaired meters drop out, new failures come in, and the later days of
yesterday's plan are replaced. Planning several days ahead still matters,
because today's routes are chosen knowing what the rest of the week has to
cover.

## Travel times: Dijkstra's Algorithm on the Bangkok road network

Every distance and travel time in the plan is measured along real roads. This
is step 3 of the workflow, and the solver prices everything afterwards with
these numbers.

The road network comes from OpenStreetMap. `tools/build_bangkok_graph.py`
takes the Thailand extract and keeps the drivable roads in Bangkok and the five
provinces around it, stored as a sparse matrix (CSR) in
`mapdata/bangkok_road_graph.npz`. Each road segment has a length in metres and
a driving time in seconds. The time comes from the OSM `maxspeed` tag when
there is one. Otherwise it depends on the road type, from 80 km/h on motorways
down to 15 km/h on service roads (trunk 60, primary 45, secondary 40, tertiary
35, unclassified 30, residential 25).

From each point, one run of Dijkstra's algorithm
(`scipy.sparse.csgraph.dijkstra`) finds the best route to every other point.
You choose on the screen what "best" means.

- *quickest* (default) minimises driving time, and the kilometres are then
  measured along that same route;
- *shortest* minimises distance, and the time is measured along that route.

So the time and the distance in a cell always describe one road. To get the
second number, the code follows the predecessor array back from each
destination and adds up the segments, one hop at a time for all destinations
at once. The results are kept on disk, so later runs only search from meters
they haven't seen before.

Map speeds are free-flow speeds, so all times are scaled by one factor until
the average speed over the run's trips equals the vehicle speed set on screen
(60 km/h by default). This doesn't change which route or plan is best. It only
moves the clock, meaning the arrival times and the minutes in Working History.

A road route can't be shorter than the straight line between its two points.
If the graph ever says otherwise, the run stops. It usually means latitude and longitude were swapped,
or a point is outside the map.

## The routing model: Google OR-Tools

### What the solver decides

The problem is a vehicle routing problem where a "vehicle" is one team on one
day. Four teams over five working days gives twenty vehicles. Each vehicle has
a job limit taken from the Working Day sheet, a start depot and an end depot.

Every pending meter is an optional visit (a disjunction in OR-Tools). The
solver may leave a meter out, but it pays a penalty for doing so. It then
minimises

```
total travel  +  sum of penalties for the meters left out
```

This is a prize-collecting VRP. Which meters to do, which team and day takes
each one, and the order of the visits all come out of one solve.

### Deadline classes and penalties

Each meter falls into one of three classes.

| Class | Meaning |
|---|---|
| Last chance (รอบบิลที่ต้องซ่อมให้ทัน) | No later plan can reach it before its reading day. Jobs marked `Urgent` in the Billing Day column also go here, on any day. |
| Advance window (รอบบิลที่จะซ่อมล่วงหน้า) | Read on dates the planner chose to prepare for now, while teams are in the area. |
| Filler | Everything else. |

Each class's penalty is larger than everything below it put together. `C` is
more travel than all the teams could do in the plan, and the penalties are set
as follows.

```
filler  = C + 1
window  = (largest filler penalty) × (number of fillers) + C + 1
last    = window × (number in window) + (largest filler) × (number of fillers) + C + 1
```

The solver therefore can't trade a last-chance meter for any number of window
meters or any amount of saved driving. The order is strict.

1. save as many last-chance readings as possible,
2. then as many advance-window meters as possible,
3. then fill every remaining slot,
4. then drive as little as possible.

All of these numbers follow from the size of the problem, so there is nothing
to tune. The worst-case total is checked against the 64-bit limit before
solving.

### Filling spare slots near the route

With every filler worth the same, teams with spare slots tended to pick meters
close to their depot, which often meant a detour away from the day's real
work. So each filler's penalty gets a small bonus for being close to a meter
that has to be visited anyway.

```
filler_i = C + 1 + (d_max − d_i)      d_i = round trip from the nearest must-do meter
```

With a fixed number of slots, this works out the same as charging each chosen
filler its side trip. These fillers show as `NEARBY` in the output.

### Search

OR-Tools builds a first solution with `PATH_CHEAPEST_ARC` and then improves it
with `GUIDED_LOCAL_SEARCH` for up to 180 seconds. A stop rule can end the
search early once it has gone a set time without a real improvement, meaning a
better choice of meters or routes that drive at least 0.1% less. That wait is
set to 180 seconds, the same as the time limit, so every run uses the full
three minutes. Two runs can still differ slightly.

### Workload balancing

The model doesn't try to make each day's workload equal, because evening out
one day would cost extra kilometres. After the solve, each day's routes are
handed out heaviest first, each to the team with the fewest kilometres per
worked day so far (from the Working History sheet). Routes are only swapped
between teams with the same job limit that day. All teams start and end at the
day's depots, so the swap doesn't change the total driving, and over time the
teams' totals even out.

### Checking the plan

Before anything is written, the program re-checks the plan from the routes
themselves. No meter may appear twice, no team may go over its limit or have
two routes on the same day, no meter may come after its reading day, and every
row must come from the input. If any check fails, the run stops and nothing is
written.

## Using the program

### Installing

You need Windows or macOS, Microsoft Excel, and Python 3.14 (any 3.11 or newer
works if you install the packages from the internet). The road graph is
already in `mapdata/` in the shared folder.

- On Windows with internet, run `py -m pip install --user -r requirements.txt`.
- On Windows without internet, for example on a network that blocks package
  downloads, double-click `offline_install/install_windows.bat`. It installs
  every package from the `offline_install/wheels` folder. Python itself isn't
  included; put the 64-bit Python 3.14 installer in that folder and the script
  runs it. See `offline_install/README.md`.
- On macOS, run `xcode-select --install`, then `bash setup_macos.sh`.

Excel itself writes the workbook (through COM on Windows, xlwings on macOS),
because openpyxl would silently drop its tables and other Excel features. The
first time on macOS, allow Python to control Excel.

### The workbook: `workforce.xlsx`

This is both the input and the output. It holds customer data, so it isn't in
the repository.

| Sheet | What goes in it |
|---|---|
| `Pending Job` | The meters to plan, already filtered by the planner, with `Customer No.`, `Lat`, `Long`, `Purpose`, `Billing Day` (the meter's reading-day code, or `Urgent`) |
| `Meter Location` | Backup coordinates for a pending job that has none |
| `Working Day` | One row per date, with `Job for Team 1` … `Job for Team N`, `Starting Branch Depot`, `Ending Branch Depot`. A row of zeros is a day off. `-` as the ending depot means the teams don't drive back that day. |
| `Branch Location` | Depot name and `Location` as `"lat, lon"` |
| `Daily Route` | Written by the program, with day 1 of the plan and one row per stop |
| `Working History` | Appended by the program, one row per team per published day |

`Customer No.` is only an identifier. It is never used to sort or rank jobs.

### Planning a day

The planning screen is in Thai, because the planners who use it every day are
Thai speakers.

1. Close `workforce.xlsx` in Excel on every computer that has it open. The file
   sits in OneDrive, and a copy left open elsewhere can save over the result.
2. Double-click `planner.bat` (Windows) or `planner.command` (macOS).
3. On the calendar, click "วันทำงาน" (work days) and pick the days the teams go
   out. The panel on the left checks your days against the Working Day sheet.
   A picked day with no jobs there is marked ✗ and has to be fixed in the sheet;
   press "อ่าน workforce.xlsx ใหม่" (reload) after editing it. Days that have
   jobs in the sheet but weren't picked are listed as a reminder.
4. Check the "รอบบิลที่ต้องซ่อมให้ทัน" (last chance) dates in the orange frame.
   They are filled in from your work days, and you can add or remove dates.
   Then pick the "รอบบิลที่จะซ่อมล่วงหน้า" (advance window) dates in light blue.
5. Choose quickest or shortest routes and the vehicle speed.
6. Leave "ทดลองคำนวณ" (trial run) ticked and press "วางแผน" (plan). A run
   usually takes three to five minutes.
7. Look at the result and open the map. If it's what you want, press
   "บันทึกผลลง Excel" (save to Excel), which writes the same plan without
   solving again. It refuses if anything in the workbook changed since the
   trial, or if you changed the days or settings on screen. You can also untick
   the trial box and run again to plan and write in one go.

Every write first copies the workbook to `backups/`, keeping the latest 10,
then reads the file back to make sure the plan landed in it.

### What you get

The Daily Route sheet has one row per stop, with the columns Date, Team,
Sequence, Customer No., Purpose, Billing Day, Days Until Reading, Billing
State, Latitude, Longitude, Estimated Distance (km), Estimated Arrival Time,
Start Depot, End Depot, and Navigation Link.

The navigation link on each team's first row opens Google Maps with the whole
route in order, from the start depot through every stop to the end depot. Excel limits a link to 255
characters, which fits six stops, so longer routes get more than one link. On a
day with no drive back, the link ends at the last stop.

Billing State is one of `URGENT` (marked by the planner), `CRITICAL` (reading
within 3 days), `UNCRITICAL`, `NEARBY` (a filler taken because it was near the
route), `MISSED` (reading day already passed) or `UNKNOWN` (billing code can't
be read).

The map opens from the "เปิดแผนที่" (open map) button, or later from
`map.bat`. It shows every route on the streets, one day at a time or all days
together, with each team's kilometres so far. Click a team to hide or show its
route. Pending jobs that no route takes are grey pins; click "Not planned" to
hide them.

### Command line

The command line can do everything the screen does.

```bash
python main.py --dry-run                     # plan and print, write nothing
python main.py --work-dates 2026-10-21,2026-10-22 \
               --last-dates 2026-10-22,2026-10-23 \
               --window-dates 2026-10-26,2026-10-27
python main.py --write-saved                 # write the last dry run
python main.py --path-metric distance        # shortest instead of quickest
python main.py --speed-kmh 40
python tools/view_map.py                     # redraw the map from Daily Route
python main.py --help                        # everything else
```

### Refreshing the road map

Every 6 to 12 months, run the following on a machine with internet and 3 to 4
GB of free memory.

```bash
pip install pyrosm
mkdir -p mapdata
curl -L -o mapdata/thailand-latest.osm.pbf https://download.geofabrik.de/asia/thailand-latest.osm.pbf
python tools/build_bangkok_graph.py --force
```

The first run after that re-measures every distance, which takes a few
minutes, because the stored distances no longer match the graph.

## Project layout

```
planner.py, planner.bat/.command   the planning screen
main.py                            command line; the screen runs this
map_opener.py, map.bat/.command    serve the route map on 127.0.0.1
dispatch/
  workforce.py      reads workforce.xlsx
  priority.py       billing codes, deadline classes, penalty bands
  workdays.py       working calendar, last-chance boundary
  plan_dates.py     calendar rules behind the planning screen
  road_graph.py     road graph, snapping, Dijkstra's algorithm
  matrix_cache.py   stores computed distances between runs
  distance.py       distance checks and speed calibration
  router.py         the OR-Tools model
  multiday.py       the multi-day plan and team assignment
  scheduler.py      builds the Daily Route table and navigation links
  audit.py          checks the plan before it is written
  history.py        Working History and the workload totals
  saved_plan.py     keeps the last trial run so it can be written later
  excel_writer.py   writes the workbook through Excel
  pipeline.py       runs the steps in order
  config.py         every setting in one place
tools/              build the road graph, draw and serve the map
offline_install/    installer for machines without internet
```

## Problems I ran into during the project

The first version gave every meter an urgency score from its reading date and
turned that score into the penalty. The scale I picked made one point of
urgency worth more than a whole day of the teams' driving, so the plan chased
the closest reading dates all over the city and distance hardly counted. Any
other scale, or a mix like "70% billing, 30% distance", would have been just as
arbitrary, and each choice gives a different plan. That is why the model now
uses the deadline classes above.

Each meter and depot is attached to its nearest road junction with a KD-tree
search. At first this could attach a meter to a small OSM road fragment that
connects to nothing, so the meter had no route to anywhere. On one real plan
this made the total distance several times longer than it should have been. I
fixed it by snapping only to junctions in the main connected network. A meter
more than 2 km from any road stops the run, since that almost always means a
wrong coordinate.

## How this was built

I chose the approach myself, including treating the choice of jobs and the routing as one
problem, using deadline classes and penalty bands in place of weighted scores,
Google OR-Tools as the solver, Dijkstra's algorithm on the real road network,
and the choice between quickest and shortest routes. I tested it against our
real data and use it in day-to-day planning. AI (Claude) was used only to help
write the code.

## Known limitations

- Speeds haven't been checked against real driving. The road-type speeds and
  the 60 km/h average are assumptions, so arrival times are on the optimistic
  side. Checking them against the teams' GPS or job timestamps is the next
  thing to do.
