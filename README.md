# Export list of owned games to a spreadsheet
This simle web app exports your owned games to an open document spreadsheet.
Inspired by Kris Ligman's [game tasting](https://unwinnable.com/2020/12/29/i-played-over-100-games-this-year-and-this-is-what-i-learned/) and steam's incredibly bare-bones library filtering tools.

Currently exports: steam store link, name, total playtime, linux playtime, mac playtime, windows playtime, app type, developers, publishers, is free, linux availability, mac availability, windows availability, supported languages, controller support, age gate, categories, genres, and release date.

Available online at [misc.untextured.space/tools/steam-games-exporter](https://misc.untextured.space/tools/steam-games-exporter)

Requires python 3.6+, depends on:
- Flask
- Flask-OpenID
- SQLAlchemy
- pyexcel-ods3
- pyexcel-xls
- pyexcel-xlsx
- requests
- pytest (for tests only)

If you have any suggestions/questions/requests, feel free to open a new github issue.
