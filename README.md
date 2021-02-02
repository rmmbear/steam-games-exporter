# Export list of owned games to a spreadsheet
This simle web app exports your owned games to an open document spreadsheet.
Inspired by Kris Ligman's [game tasting](https://unwinnable.com/2020/12/29/i-played-over-100-games-this-year-and-this-is-what-i-learned/) and steam's incredibly bare-bones library filtering tools.

Currently exported columns: steam store link, name, total playtime, linux playtime, mac playtime, and windows playtime.

Written in python, uses flask, flask-openid, requests, and pyexcel.

Available online at [misc.untextured.space/tools/steam-games-exporter](https://misc.untextured.space/tools/steam-games-exporter)


### Planned features:
* ~~csv, xlsx, and xls export~~ Done!
* expand metadata with info from steam store api
