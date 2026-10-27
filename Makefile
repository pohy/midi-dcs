dist/midi_joy.exe: $(wildcard *.py)
	pyinstaller --onefile --name midi_joy --hidden-import mido.backends.rtmidi midi_joy.py
