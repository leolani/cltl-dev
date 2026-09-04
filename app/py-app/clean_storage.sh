#!/bin/bash

# ASR implementation writes this files for some unknown reason
rm -f *.wav

rm -f storage/event_log/*.json

rm -f storage/audio/*.wav
rm -f storage/audio/*.json

rm -f storage/image/*.png
rm -f storage/image/*.json
rm -f storage/image/*.pkl

rm -rf storage/emissor/**/*
rmdir storage/emissor/*

# CachedImageStorage/CachedAudioStorage create the PARENT of their storage path,
# not the path itself, so a missing directory here makes the first write fail —
# a 500 on the chat UI's first image upload, and a silent retry loop for audio.
mkdir -p storage/audio storage/image storage/emissor storage/event_log
