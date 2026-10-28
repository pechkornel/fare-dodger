# Fare Dodger

A hidden-movement chase game played on the real Budapest public transport network
(Hungarian title: *Bliccelő*). One hidden **Fare Dodger** tries to stay ahead of three
**Inspectors** for 12 rounds; every round is 10 minutes of real weekday timetable.

> Status: Phase 1 (rules engine, transit routing, bots, simulation). No UI yet.

## Data source

Timetable data: [BKK Budapesti Közlekedési Központ](https://bkk.hu/) public GTFS feed
(`https://go.bkk.hu/api/static/v1/public-gtfs/budapest_gtfs.zip`). The raw data is
downloaded on demand and never committed. This project is not affiliated with or
endorsed by BKK.

## License

MIT, see [LICENSE](LICENSE).
