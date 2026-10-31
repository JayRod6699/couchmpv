# couchmpv
A TV Video Player for Linux based on libmpv optimized for tv remote use.


**Dependencies:** python-gobject gtk3 mpv python-mpv python-cairo, otf-font-awesome (optional for the osd icons).


**Usage:**
Arrows Left/Right: seek video 20 seconds.

Arrow Up: change/disable subtitle track.

Arrow Down: change/disable audio track.

OK/Enter/Space: play/pause the video.

Back/Esc/Backspace: close the video or go to the previous screen.

Home-Button: minimizes the player and go back to your Desktop Environment and resume where you left when you open the app again.


**Features:**
A nice File Browser interface for you to search your video files and play. You can add/remove any directory to the *config* file located in ~/.config/couchmpv/config.

Watch History, it keeps track of all your played videos, the exact minute you stopped so it resumes where you left previously, it also marks a video as watched if you played 90% of the video and adds a ✓ icon so you know you finished the video, the watch history is located in *~/.config/couchmpv/history.json* basically just a JSOn file, you can delete it to reset the watch history if you want.

It's based on MPV so you can adjust the settings just like it in the *config* file.
e.g:
sub-font-size=52
sub-font=sans-serif
alang=en,eng

Banner and Poster support, any picture files matching the name of the video file or folder's name, will be recognized and displayed, to make the picture be recognized as a Poster, it needs to have "_Poster" at the end of the file name, to be recognized as a Banner, it should end with "_Banner" e.g: Pulp_Fiction_Banner.jpg.

NFO Support, to show information such as Title, Release Year, Plot, you can add an NFO matching the video file's name or folder's name. example:
Pulp_Fiction.nfo (matching Pulp_Fiction.mkv).
XML Syntax:
```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<tvshow>
  <title>NameOfTheShow</title>
  <year>2010</year>
  <plot>showplot.</plot>
</tvshow>
```
