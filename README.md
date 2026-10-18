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
A nice File Browser interface for you to search your video files and play. You can add/remove any directory to the *couchmpv.py* in the DIRECTORIES section around line 46 of the code e.g: *os.path.expanduser("~/Movies"),*

Watch History, it keeps track to all your played videos, the exact minute you stopped so it resumes where you left previously, it also marks a video as watched if you played 90% of the video and adds a ✓ icon so you know you finished the video, the watch history is located in *~/.config/couchmpv/history.json* basically just a JSOn file, you can delete it to reset the watch history if you want.

It's based on MPV so you can adjust the settings just like it in the *couchmpv.py*, around the line 748, or just search for "PLAYER SETTINGS" with your text editor/IDE and use the self.player call to the setting you want, e.g: "self.player['brightness'] = 5" Know what you are doing!
