from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
from extensions import db
from models import Track, User
from forms import TrackForm
import os
import librosa
import numpy as np
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
from flask import send_from_directory
import subprocess
import uuid
from pathlib import Path
import threading
import time
import requests as http_requests
from dotenv import load_dotenv
from together import Together
import ssl
import shutil
import gc
import torch
from demucs.pretrained import get_model
from demucs.apply import apply_model
import torchaudio
import warnings
warnings.filterwarnings("ignore")
ssl._create_default_https_context = ssl._create_unverified_context
import traceback
import demucs.separate




load_dotenv()
client = Together(api_key=os.getenv('TOGETHER_API_KEY'))  # Pass the API key explicitly

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///music.db'
app.config['UPLOAD_FOLDER'] = 'static/uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
app.config['CONVERTED_FOLDER'] = 'static/converted'
os.makedirs(app.config['CONVERTED_FOLDER'], exist_ok=True)

db.init_app(app)

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'admin'



# Admin required decorator
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Admin access required.", "danger")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))



def convert_audio(input_path, output_path, output_format):
    """Convert audio file to specified format using ffmpeg"""
    try:
        result = subprocess.run([
            'ffmpeg', '-i', input_path,
            '-y',  # Overwrite output file if it exists
            output_path
        ], check=True, capture_output=True, text=True)
        print(f"Conversion output: {result.stdout}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"FFmpeg conversion error: {e.stderr}")
        return False
    except Exception as e:
        print(f"General conversion error: {str(e)}")
        return False

# Routes

import gc  # Add this import at the top

@app.route('/analyze', methods=['GET', 'POST'])
def analyze_audio():
    if request.method == 'POST':
        if 'audio_file' not in request.files:
            return jsonify({
                'success': False,
                'error': 'No file selected'
            }), 400

        audio_file = request.files['audio_file']
        if audio_file.filename == '':
            return jsonify({
                'success': False,
                'error': 'No file selected'
            }), 400

        # Check file size - limit to 10MB
        audio_file.seek(0, os.SEEK_END)
        file_size = audio_file.tell()
        audio_file.seek(0)
        
        if file_size > 10 * 1024 * 1024:  # 10MB limit
            return jsonify({
                'success': False,
                'error': 'File size too large. Please upload a file smaller than 10MB'
            }), 400

        file_uuid = str(uuid.uuid4())
        original_filename = secure_filename(audio_file.filename)
        input_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_uuid}_{original_filename}")
        
        try:
            audio_file.save(input_path)
            
            # Load the audio file with librosa using a lower sample rate and mono
            y, sr = librosa.load(input_path, sr=22050, mono=True)
            
            # Free up memory from the raw audio file
            del audio_file
            gc.collect()
            
            # Get onset envelope with reduced complexity
            onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=512)
            
            # Free up memory from raw audio data
            del y
            gc.collect()
            
            # Dynamic tempo detection with simplified parameters
            dtempo = librosa.beat.tempo(onset_envelope=onset_env, sr=sr, aggregate=None,
                                      hop_length=512, start_bpm=120)
            
            # Calculate tempos more efficiently
            tempo_frequencies = np.bincount(np.round(dtempo).astype(int))
            possible_tempos = np.where(tempo_frequencies > 0)[0]
            tempo_strengths = tempo_frequencies[possible_tempos]
            
            # Free up memory
            del onset_env
            del dtempo
            gc.collect()
            
            # Find the most likely tempo
            tempo_candidates = []
            for tempo in possible_tempos:
                score = (tempo_frequencies[tempo] if tempo < len(tempo_frequencies) else 0)
                score += (tempo_frequencies[tempo//2] if tempo//2 < len(tempo_frequencies) else 0)
                score += (tempo_frequencies[tempo*2] if tempo*2 < len(tempo_frequencies) else 0)
                tempo_candidates.append((tempo, score))
            
            # Get the best tempo
            best_tempo = sorted(tempo_candidates, key=lambda x: x[1], reverse=True)[0][0]
            
            # Load audio again for key detection with very low duration
            y, sr = librosa.load(input_path, sr=22050, duration=30, mono=True)
            
            # Detect key with simplified parameters
            chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512, n_chroma=12)
            key_names = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
            key = key_names[np.argmax(np.mean(chroma, axis=1))]
            
            # Clean up
            del y
            del chroma
            gc.collect()
            
            # Remove input file
            if os.path.exists(input_path):
                os.remove(input_path)
            
            return jsonify({
                'success': True,
                'tempo': int(round(float(best_tempo))),
                'key': key
            })
            
        except Exception as e:
            if os.path.exists(input_path):
                os.remove(input_path)
            print(f"Analysis error: {str(e)}")
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500
        finally:
            # Final cleanup
            gc.collect()

    latest_track = Track.query.order_by(Track.date_added.desc()).first()
    return render_template('analyze.html', latest_track=latest_track)

@app.route('/separator', methods=['GET', 'POST'])
def stem_separator():
   if request.method == 'POST':
       if 'audio_file' not in request.files:
           return jsonify({
               'success': False,
               'error': 'No file selected'
           }), 400

       audio_file = request.files['audio_file']
       if audio_file.filename == '':
           return jsonify({
               'success': False,
               'error': 'No file selected'
           }), 400

       # Verify file type 
       if not audio_file.filename.lower().endswith(('.mp3', '.wav', '.m4a', '.flac')):
           return jsonify({
               'success': False,
               'error': 'Invalid file type. Please upload an MP3, WAV, M4A, or FLAC file.'
           }), 400

       # Check file size - 15MB limit
       file_size = 0
       audio_file.seek(0, os.SEEK_END)
       file_size = audio_file.tell()
       audio_file.seek(0)
       
       if file_size > 15 * 1024 * 1024:
           return jsonify({
               'success': False,
               'error': 'File too large. Please upload a file smaller than 15MB'
           }), 400

       # Create unique filenames
       file_uuid = str(uuid.uuid4())
       original_filename = secure_filename(audio_file.filename)
       input_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_uuid}_{original_filename}")
       output_dir = file_uuid + "_" + os.path.splitext(original_filename)[0]
       
       try:
           # Save input file
           audio_file.save(input_path)
           print(f"File saved: {input_path}")

           # Clear memory
           del audio_file
           gc.collect()
           
           # Configure demucs for separation with correct parameters
           demucs.separate.main([
               "--mp3",
               "-n", "htdemucs",
               "--segment", "7",
               "-d", "cpu",
               "--overlap", "0.1",
               "--out", app.config['CONVERTED_FOLDER'],
               input_path
           ])
           
           # Generate URLs for stems
           stem_paths = {}
           source_stems = ['drums', 'bass', 'vocals', 'other']
           display_stems = ['drums', 'bass', 'vocals', 'melody']
           
           # Find output directory
           possible_dirs = [
               output_dir,
               file_uuid,
               os.path.splitext(original_filename)[0]
           ]
           
           found_dir = None
           for dir_name in possible_dirs:
               check_path = os.path.join(app.config['CONVERTED_FOLDER'], 'htdemucs', dir_name)
               if os.path.exists(check_path):
                   found_dir = dir_name
                   break

           if not found_dir:
               raise Exception("Output directory not found")
           
           # Get stem URLs
           for source_stem, display_stem in zip(source_stems, display_stems):
               stem_filename = f"{source_stem}.mp3"
               full_path = os.path.join(app.config['CONVERTED_FOLDER'], 'htdemucs', found_dir, stem_filename)
               if os.path.exists(full_path):
                   relative_path = os.path.join('htdemucs', found_dir, stem_filename)
                   stem_paths[display_stem] = url_for('static', filename=f'converted/{relative_path}')
                   print(f"Generated URL for {display_stem} stem")

           # Clean up input file
           if os.path.exists(input_path):
               os.remove(input_path)
               print("Input file cleaned up")
           
           # Force garbage collection
           gc.collect()
           if torch.cuda.is_available():
               torch.cuda.empty_cache()
           
           return jsonify({
               'success': True,
               'stems': stem_paths,
               'session_id': found_dir
           })
           
       except Exception as e:
           # Clean up input file in case of error
           if os.path.exists(input_path):
               os.remove(input_path)
           print(f"Separation error: {str(e)}")
           print(f"Full error details: {traceback.format_exc()}")
           return jsonify({
               'success': False,
               'error': 'Failed to process audio file. Please try again with a different file.'
           }), 500

   latest_track = Track.query.order_by(Track.date_added.desc()).first()
   return render_template('separator.html', latest_track=latest_track)


@app.route('/cleanup_stems/<session_id>', methods=['POST'])
def cleanup_stems(session_id):
   try:
       output_dir = os.path.join(app.config['CONVERTED_FOLDER'], 'htdemucs', session_id)
       if os.path.exists(output_dir):
           shutil.rmtree(output_dir)
           return jsonify({'success': True})
       return jsonify({'success': True, 'message': 'Directory already cleaned'})
   except Exception as e:
       print(f"Cleanup error: {str(e)}")
       return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/guides')
def guides():
    latest_track = Track.query.order_by(Track.date_added.desc()).first()
    return render_template('guides.html', latest_track=latest_track)

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.get_json()
    user_message = data.get('message', '')
    
    try:
        response = client.chat.completions.create(
            model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
            messages=[
                {
                    "role": "system",
                    "content": "Your name is Alex. You are a music production expert who helps people learn about music production, DAWs, mixing, and music theory, especially hip hop beatmaking. Try not to repeat yourself too much and be funny sometimes. Make the experience of learning music production and hip hop beats as fun as possible"
                },
                {
                    "role": "user",
                    "content": user_message
                }
            ],
            temperature=0.7,
            top_p=0.7,
            top_k=50,
            repetition_penalty=1
        )
        
        answer = response.choices[0].message.content
        return jsonify({"answer": answer})
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return jsonify({"answer": "Sorry, I encountered an error. Please try again."}), 500

@app.route('/')
def index():
    latest_track = Track.query.order_by(Track.date_added.desc()).first()
    return render_template('home.html', latest_track=latest_track)

@app.route('/about')
def about():
    latest_track = Track.query.order_by(Track.date_added.desc()).first()
    return render_template('about.html', latest_track=latest_track)

@app.route('/showcase')
def showcase():
    latest_track = Track.query.order_by(Track.date_added.desc()).first()
    sort_by = request.args.get('sort', 'name_asc')
    
    if sort_by == 'name_asc':
        tracks = Track.query.order_by(Track.name.asc()).all()
    elif sort_by == 'name_desc':
        tracks = Track.query.order_by(Track.name.desc()).all()
    elif sort_by == 'date_asc':
        tracks = Track.query.order_by(Track.date_added.asc()).all()
    elif sort_by == 'date_desc':
        tracks = Track.query.order_by(Track.date_added.desc()).all()
    elif sort_by == 'play_count':
        tracks = Track.query.order_by(Track.play_count.desc()).all()
    else:
        tracks = Track.query.all()
        
    return render_template('showcase.html', tracks=tracks, sort_by=sort_by, latest_track=latest_track)

@app.route('/converter', methods=['GET', 'POST'])
def converter():
    if request.method == 'POST':
        input_path = None
        output_path = None
        try:
            if 'audio_file' not in request.files:
                return jsonify({
                    'success': False,
                    'error': 'No file selected'
                }), 400
                
            audio_file = request.files['audio_file']
            if audio_file.filename == '':
                return jsonify({
                    'success': False,
                    'error': 'No file selected'
                }), 400

            target_format = request.form.get('target_format')
            if target_format not in ['mp3', 'wav', 'flac']:
                return jsonify({
                    'success': False,
                    'error': 'Invalid format selected'
                }), 400

            # Generate unique identifier and get original filename
            file_uuid = str(uuid.uuid4())
            original_filename = secure_filename(audio_file.filename)
            
            # Save input file with UUID
            input_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_uuid}_{original_filename}")
            audio_file.save(input_path)

            # Create output path - keep UUID for server storage but use original name for download
            original_name = os.path.splitext(original_filename)[0]
            output_filename = f"{original_name}.{target_format}"  # This is what user will see
            server_output_filename = f"{file_uuid}_{output_filename}"  # This is for server storage
            output_path = os.path.join(app.config['CONVERTED_FOLDER'], server_output_filename)

            # Convert file
            if convert_audio(input_path, output_path, target_format):
                # Generate download URL - use original name for download
                download_url = url_for('static', 
                                     filename=f'converted/{server_output_filename}')

                # Set up timeout for file deletion
                def delete_file():
                    time.sleep(15)  # Wait 15 seconds
                    try:
                        if os.path.exists(output_path):
                            os.remove(output_path)
                            print(f"Cleaned up converted file: {output_path}")
                    except Exception as e:
                        print(f"Error cleaning up converted file: {str(e)}")

                # Start deletion timer in background
                cleanup_thread = threading.Thread(target=delete_file)
                cleanup_thread.daemon = True
                cleanup_thread.start()

                return jsonify({
                    'success': True,
                    'download_url': download_url,
                    'filename': output_filename  # Send just the clean filename
                })
            else:
                return jsonify({
                    'success': False,
                    'error': 'Conversion failed'
                }), 500

        except Exception as e:
            print(f"Conversion error: {str(e)}")
            return jsonify({
                'success': False,
                'error': 'Server error occurred'
            }), 500
        
        finally:
            # Clean up input file
            if input_path and os.path.exists(input_path):
                try:
                    os.remove(input_path)
                    print(f"Cleaned up input file: {input_path}")
                except Exception as e:
                    print(f"Error cleaning up input file: {str(e)}")

    latest_track = Track.query.order_by(Track.date_added.desc()).first()
    return render_template('converter.html', latest_track=latest_track)

@app.route('/admin', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_panel'))

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username, is_admin=True).first()

        if user and check_password_hash(user.password, password):
            login_user(user)
            flash("Login successful!", "success")
            return redirect(url_for('admin_panel'))
        else:
            flash("Invalid admin credentials.", "danger")

    latest_track = Track.query.order_by(Track.date_added.desc()).first()
    return render_template('login.html', latest_track=latest_track)

@app.route('/admin/panel', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_panel():
    tracks = Track.query.order_by(Track.date_added.desc()).all()
    latest_track = tracks[0] if tracks else None
    form = TrackForm()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add' and form.validate_on_submit():
            # Get the track name and create a safe filename
            track_name = form.name.data
            safe_name = secure_filename(track_name)
            
            new_track = Track(
                name=track_name,
                description=form.description.data or ""
            )

            # Handle audio file
            if 'file' in request.files and request.files['file']:
                music_file = request.files['file']
                file_ext = os.path.splitext(music_file.filename)[1]
                music_filename = safe_name + file_ext
                music_file.save(os.path.join(app.config['UPLOAD_FOLDER'], music_filename))
                new_track.file = music_filename

            # Handle primary artwork
            if 'artwork' in request.files and request.files['artwork']:
                artwork = request.files['artwork']
                art_ext = os.path.splitext(artwork.filename)[1]
                artwork_filename = safe_name + "_artwork" + art_ext
                artwork.save(os.path.join(app.config['UPLOAD_FOLDER'], artwork_filename))
                new_track.artwork = artwork_filename
            else:
                new_track.artwork = "No Artwork"

            # Handle secondary artwork
            if 'artwork_secondary' in request.files and request.files['artwork_secondary']:
                secondary = request.files['artwork_secondary']
                sec_ext = os.path.splitext(secondary.filename)[1]
                secondary_filename = safe_name + "_secondary" + sec_ext
                secondary.save(os.path.join(app.config['UPLOAD_FOLDER'], secondary_filename))
                new_track.artwork_secondary = secondary_filename
            else:
                new_track.artwork_secondary = "No Secondary Artwork"

            db.session.add(new_track)
            db.session.commit()
            flash('New track added successfully!', 'success')

        elif action == 'update':
            track_id = request.form.get('track_id')
            track = Track.query.get_or_404(track_id)
            
            if track:
                track_name = request.form.get('name')
                safe_name = secure_filename(track_name)
                
                track.name = track_name
                track.description = request.form.get('description', track.description)

                # Handle audio file update
                if 'file' in request.files and request.files['file'].filename != '':
                    music_file = request.files['file']
                    if track.file:
                        old_file_path = os.path.join(app.config['UPLOAD_FOLDER'], track.file)
                        if os.path.exists(old_file_path):
                            os.remove(old_file_path)
                    file_ext = os.path.splitext(music_file.filename)[1]
                    music_filename = safe_name + file_ext
                    music_file.save(os.path.join(app.config['UPLOAD_FOLDER'], music_filename))
                    track.file = music_filename

                # Handle primary artwork update
                if 'artwork' in request.files and request.files['artwork'].filename != '':
                    artwork = request.files['artwork']
                    if track.artwork and track.artwork != "No Artwork":
                        old_artwork_path = os.path.join(app.config['UPLOAD_FOLDER'], track.artwork)
                        if os.path.exists(old_artwork_path):
                            os.remove(old_artwork_path)
                    art_ext = os.path.splitext(artwork.filename)[1]
                    artwork_filename = safe_name + "_artwork" + art_ext
                    artwork.save(os.path.join(app.config['UPLOAD_FOLDER'], artwork_filename))
                    track.artwork = artwork_filename

                # Handle secondary artwork update
                if 'artwork_secondary' in request.files and request.files['artwork_secondary'].filename != '':
                    secondary = request.files['artwork_secondary']
                    if track.artwork_secondary and track.artwork_secondary != "No Secondary Artwork":
                        old_secondary_path = os.path.join(app.config['UPLOAD_FOLDER'], track.artwork_secondary)
                        if os.path.exists(old_secondary_path):
                            os.remove(old_secondary_path)
                    sec_ext = os.path.splitext(secondary.filename)[1]
                    secondary_filename = safe_name + "_secondary" + sec_ext
                    secondary.save(os.path.join(app.config['UPLOAD_FOLDER'], secondary_filename))
                    track.artwork_secondary = secondary_filename

                db.session.commit()
                flash('Track updated successfully!', 'success')

        return redirect(url_for('admin_panel'))

    return render_template('admin.html', tracks=tracks, form=form, latest_track=latest_track)

@app.route('/download_tracks', methods=['POST'])
@login_required
@admin_required
def download_tracks():
    try:
        data = request.get_json()
        track_ids = data.get('track_ids', [])
        
        if not track_ids:
            return jsonify({'success': False, 'message': 'No tracks selected'}), 400
            
        tracks = Track.query.filter(Track.id.in_(track_ids)).all()
        files_info = []
        
        for track in tracks:
            if track.file:
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], track.file)
                if os.path.exists(file_path):
                    files_info.append({
                        'name': track.name,
                        'url': url_for('static', filename=f'uploads/{track.file}', _external=True)
                    })

        if not files_info:
            return jsonify({'success': False, 'message': 'No files available for download'}), 404

        return jsonify({
            'success': True,
            'files': files_info,
            'message': 'Files ready for download'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/delete_tracks', methods=['POST'])
@login_required
@admin_required
def delete_tracks():
    data = request.get_json()
    track_ids = data.get('track_ids', [])
    
    try:
        for track_id in track_ids:
            track = Track.query.get_or_404(track_id)
            
            # Delete associated files
            if track.artwork and track.artwork != "No Artwork":
                artwork_path = os.path.join(app.config['UPLOAD_FOLDER'], track.artwork)
                if os.path.exists(artwork_path):
                    os.remove(artwork_path)
                    
            if track.file:
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], track.file)
                if os.path.exists(file_path):
                    os.remove(file_path)

            db.session.delete(track)
        
        db.session.commit()
        return jsonify({'success': True, 'message': 'Tracks deleted successfully!'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("You have logged out.", "info")
    return redirect(url_for('index'))

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # Create admin user if it doesn't exist
        admin_user = User.query.filter_by(username='Nobz').first()
        if not admin_user:
            admin_user = User(
                username='Nobz',
                password=generate_password_hash('LETmeinnow36$'),
                is_admin=True
            )
            db.session.add(admin_user)
            db.session.commit()
    app.run(debug=True)