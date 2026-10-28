dist/midi_dcs.exe: $(wildcard *.py)
	pyinstaller --onefile --name midi_dcs --hidden-import mido.backends.rtmidi midi_dcs.py
