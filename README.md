# GroupMe Calendar Manager Agent

A Google Cloud Function that manages a calendar through GroupMe group chat. Users can create, update, delete, and list calendar events through natural language messages. The bot automatically detects scheduling conflicts and suggests alternate times.

## Features

### Core Calendar Operations
- **Create Events**: "create event Movie night on Friday at 7pm"
- **Update Events**: "update event change Movie night to Saturday"
- **Delete Events**: "delete event Movie night"
- **List Events**: "list events" or "show upcoming events"
- **Conflict Detection**: Automatically checks for scheduling conflicts before creating/updating events
- **Alternate Suggestions**: Suggests available time slots if a conflict is detected

### User Management
- **Approved Users List**: Only approved users can modify the calendar
- **Admin Commands**: Manage approved users dynamically
- **Admin-Only Controls**: Subset of admins can manage the approved user list and admin list

### Admin Commands
- `calendar admin add <user_id>` - Add user to approved list
- `calendar admin remove <user_id>` - Remove user from approved list
- `calendar admin list` - Show approved users
- `calendar admin admins add <user_id>` - Add admin (admins only)
- `calendar admin admins remove <user_id>` - Remove admin (admins only)
- `calendar admin admins` - List all admins

### Help Command
- `calendar help` or `cal help` - Display help information

## Configuration

### Environment Variables
- `GOOGLE_CALENDAR_ID`: Primary calendar ID for event storage
- `GROUPME_BOT_ID`: GroupMe bot ID for sending messages
- `GROUPME_ACCESS_TOKEN`: GroupMe API access token

### Firestore Collection Structure
```
calagent/
├── {group_id}/
│   ├── approvedUsers: {user_id: true, ...}
│   ├── admins: {user_id: true, ...}
│   └── calendarId: "string"
```

## Message Format (from GroupMe)
```json
{
  "text": "create event Movie night on Friday at 7pm",
  "user_id": "user123",
  "group_id": "group456"
}
```

## Deployment

```bash
gcloud functions deploy calendar_agent \
  --runtime python312 \
  --trigger-http \
  --entry-point calendar_agent \
  --set-env-vars GOOGLE_CALENDAR_ID=your-calendar-id \
  --allow-unauthenticated
```

## Integration with Google Calendar MCP

The function is designed to work with the Google Calendar Model Context Protocol (MCP):
- Uses MCP tools to query and modify calendar events
- Respects MCP constraints for availability checking
- Integrates MCP conflict detection mechanisms

## TODO Items

1. **Google Calendar MCP Integration**:
   - `parse_event_details()`: Use MCP for NLP event parsing
   - `check_calendar_conflicts()`: Integrate MCP conflict detection
   - `suggest_alternate_times()`: Use MCP to find available slots
   - Calendar CRUD operations (create, update, delete, list)

2. **Firestore Integration**:
   - Implement persistent storage for approved users and admins
   - Store group-specific calendar configurations

3. **GroupMe Integration**:
   - Implement `send_groupme_message()` with actual API calls
   - Add message formatting with GroupMe markdown
   - Handle bot token management securely

4. **Enhanced NLP**:
   - Add support for recurring events
   - Improve date/time parsing with timezone support
   - Add event description extraction
   - Support for location and attendee information

5. **Additional Features**:
   - RSVP functionality
   - Event reminders
   - Ical export/import
   - Integration with Google Meet for video calls
