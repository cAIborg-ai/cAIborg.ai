# WARP.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

This is a static website project hosting the "Dopamine Timer" - a gamified Pomodoro timer that rewards users with YouTube videos. The project consists of a single-page application built with vanilla HTML, CSS, and JavaScript, hosted via GitHub Pages at caiborg.ai.

## Development Commands

### Local Development
```bash
# Serve the site locally (using Python's built-in server)
python -m http.server 8000

# Alternative using Node.js (if npx is available)
npx http-server -p 8000

# Alternative using PHP (if available)
php -S localhost:8000
```

### Git Operations
```bash
# Check repository status
git status

# View recent commits
git log --oneline -n 10

# Deploy changes (pushes to main trigger GitHub Pages deployment)
git add .
git commit -m "Update timer features"
git push origin main
```

### File Operations
```bash
# Validate HTML structure
# Note: No build process needed - direct HTML file

# Check file structure
ls -la

# View current domain configuration
cat CNAME
```

## Architecture & Code Structure

### Single-File Architecture
- **`index.html`**: Contains the entire application in one file
  - HTML structure (lines 1-256)
  - CSS styling (lines 7-252) 
  - JavaScript functionality (lines 316-554)

### Key Application Components

#### Timer System (JavaScript lines 317-330)
- State management for timer intervals and remaining seconds
- Statistics tracking for completed sprints and rewards earned
- Local storage persistence for user data

#### YouTube Integration (lines 331-366)
- YouTube IFrame API integration for reward videos
- Random video selection from user-configured playlist
- Overlay system for video playback experience

#### Gamification Engine (lines 477-541)
- Probability-based reward system (configurable percentage)
- Statistics tracking (sprints completed, rewards earned, win rate, total time)
- Visual feedback system with animations and notifications

#### Settings & Persistence (lines 368-400)
- Local storage for user preferences and statistics
- Configurable sprint duration (1-120 minutes)
- Customizable reward probability (0-100%)
- User-defined YouTube playlist management

### Styling Architecture
- CSS custom properties for theming (cyberpunk aesthetic)
- Gradient-based design system with neon colors
- Responsive grid layout for statistics display
- Animation system using CSS keyframes for feedback

## Development Patterns

### State Management Pattern
The application uses a simple global state pattern with localStorage persistence:
- Timer state (running, paused, remaining time)
- User statistics (sprints, rewards, total time)  
- Settings (duration, probability, YouTube links)

### Event-Driven Architecture
DOM event listeners handle user interactions:
- Timer controls (start/pause/reset)
- Settings updates with immediate persistence
- Video player lifecycle management

### Modular Function Design
Functions are organized by responsibility:
- Timer operations (start/pause/reset/complete)
- Settings management (load/save/update)
- Statistics tracking and display
- YouTube integration and video handling

## Deployment

This project uses GitHub Pages for hosting:
- **Domain**: Custom domain configured via CNAME file
- **Branch**: Deploys from `main` branch automatically  
- **URL**: https://caiborg.ai
- **Build**: No build process required (static files)

### Making Changes
1. Edit `index.html` directly in the repository
2. Test changes locally using a simple HTTP server
3. Commit and push to `main` branch
4. GitHub Pages automatically updates the live site

## Key Implementation Details

### YouTube API Integration
- Loads YouTube IFrame API dynamically
- Handles player ready state and video end events  
- Extracts video IDs from various YouTube URL formats
- Implements audio initialization workaround for autoplay policies

### Local Storage Schema
```javascript
// Settings object
{
  sprintDuration: number,    // minutes (1-120)
  rewardProbability: number, // percentage (0-100)  
  youtubeLinks: string       // newline-separated URLs
}

// Statistics object
{
  completedSprints: number,
  rewardsEarned: number,
  totalMinutes: number
}
```

### Reward System Logic
1. User completes a sprint (timer reaches zero)
2. System generates random number (0-100)
3. If random number < configured probability, trigger reward
4. Reward shows notification animation then plays random YouTube video
5. Statistics updated and persisted to localStorage
