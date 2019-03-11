/******************************************************************************
Heart_Rate_Display.ino
Demo Program for AD8232 Heart Rate sensor.
Casey Kuhns @ SparkFun Electronics
6/27/2014
https://github.com/sparkfun/AD8232_Heart_Rate_Monitor

The AD8232 Heart Rate sensor is a low cost EKG/ECG sensor.  This example shows
how to create an ECG with real time display.  The display is using Processing.
This sketch is based heavily on the Graphing Tutorial provided in the Arduino
IDE. http://www.arduino.cc/en/Tutorial/Graph

Resources:
This program requires a Processing sketch to view the data in real time.

Development environment specifics:
	IDE: Arduino 1.0.5
	Hardware Platform: Arduino Pro 3.3V/8MHz
	AD8232 Heart Monitor Version: 1.0

This code is beerware. If you see me (or any other SparkFun employee) at the
local pub, and you've found our code helpful, please buy us a round!

Distributed as-is; no warranty is given.
******************************************************************************/

import processing.serial.*;

Serial myPort;        // The serial port
int xPos = 1;         // horizontal position of the graph
float height_old = 0;
float height_new = 0;
float inByte = 0;
int cBPM = 0;
int BPM = 0;
int beat_old = 0;
float[] beats = new float[500];  // Used to calculate average BPM
int beatIndex;
float threshold = 620.0;  //Threshold at which BPM calculation occurs
boolean belowThreshold = true;
PFont font;
boolean toBeDrawn = false;


void setup () {
  // set the window size:
  size(2500, 1000);        

  // List all the available serial ports
  println(Serial.list());  
  // Open whatever port is the one you're using.
  myPort = new Serial(this, Serial.list()[2], 9600);
  // don't generate a serialEvent() unless you get a newline character:
  myPort.bufferUntil('\n');
  // set inital background:
  background(0xff);
  font = createFont("Arial", 12, true);
  oneTimeDraw = true;
  startDraw = millis();
}

int startDraw = 0;
boolean oneTimeDraw;

void draw () {

  //Map and draw the line for new data point     
     //inByte = map(inByte, 0, 1023, 0, height);     
     height_new = height - inByte;
     int middle = height - 512;
     strokeWeight(1.8);
     line(xPos - 1, height_old, xPos, height_new);
     height_old = height_new;
    
      // at the edge of the screen, go back to the beginning:
      if (xPos >= width) {
        xPos = 0;
        
        saveFrame("ECG-Dan-######.png");
        
        background(0xff);
        int totalMillisForWidth = millis() - startDraw;
        startDraw = millis();
        
        int cubeEdge = (int)(40 * width/totalMillisForWidth);        
        
        
        for (int px = middle; px > 0; px--)
        {
          int y = middle - px;
          if (y % (5 * cubeEdge) == 0)
          {
            stroke(0, 0, 0);
            strokeWeight(0.8);
            line(0, px, width, px);
          }
          else
          {if (y % cubeEdge == 0)
          {
            stroke(0, 0, 0xff); //Set stroke to red ( R, G, B)
            strokeWeight(0.1);
            line(0, px, width, px);
          }}          
        }
        
        for (int px = 0; px < height - middle; px = px + cubeEdge)
        {
          int y = middle + px;
          if (px % (cubeEdge * 5) == 0)
          {
            stroke(0, 0, 0);
            strokeWeight(0.8);            
            line(0, y, width, y);
          }
          else
          {if (px % cubeEdge == 0)
          {
            stroke(0, 0, 0xff); //Set stroke to red ( R, G, B)
            strokeWeight(0.1);            
            line(0, y, width, y);
          }}          
        }
        
        
        for (int m = 0; m < totalMillisForWidth; m++)
        {
            int positionm = (int)(m * width/totalMillisForWidth);
            if (m % 200 == 0)
            {
              stroke(0, 0, 0); //Set stroke to red ( R, G, B)
              strokeWeight(0.8);
              line(positionm, 0, positionm, height);
              stroke(0xff, 0, 0); //Set stroke to red ( R, G, B)
              strokeWeight(1);
            }
            else{
              if (m % 40 == 0)
              {
                strokeWeight(0.1);
                stroke(0, 0, 0xff); //Set stroke to red ( R, G, B)
                line(positionm, 0, positionm, height);
                stroke(0xff, 0, 0); //Set stroke to red ( R, G, B)
                strokeWeight(1);
              }
            }            
        }
        
      } 
      else {
        // increment the horizontal position:
        xPos = xPos + 2;        
      }
      
      if (toBeDrawn)
      {
        textSize(24);
        text("♥ " + cBPM, xPos - 20, 300);
        toBeDrawn = false;
      }
      
      // draw text for BPM periodically
      if (millis() % 128 == 0){
        fill(0xFF);
        rect(0, 0, 880, 25);
        fill(0x00);
        textSize(12);
        text("data: " + inByte, 615, 14);
        text("Current ♥ " + cBPM, 15, 14);
        
        if (oneTimeDraw)
        {
          textSize(16);
          text("Grid draws after one pass CALIBRATION completes!", xPos - 20, 200);
          oneTimeDraw = false;
        }
        
        
      }      
       
}


void serialEvent (Serial myPort) 
{
  // get the ASCII string:
  String inString = myPort.readStringUntil('\n');

  if (inString != null) 
  {
    // trim off any whitespace:
    inString = trim(inString);

    // If leads off detection is true notify with blue line
    if (inString.equals("!")) 
    { 
      stroke(0, 0, 0xff); //Set stroke to blue ( R, G, B)
      inByte = 512;  // middle of the ADC range (Flat Line)
    }
    // If the data is good let it through
    else 
    {
      stroke(0xff, 0, 0); //Set stroke to red ( R, G, B)
      inByte = float(inString); 
      
      // BPM calculation check
      if (inByte > threshold && belowThreshold == true)
      {
        toBeDrawn = true;
        calculateBPM();
        belowThreshold = false;        
      }
      else if(inByte < threshold)
      {
        belowThreshold = true;
      }
    }
  }
}
  
void calculateBPM () 
{  
  int beat_new = millis();    // get the current millisecond
  int diff = beat_new - beat_old;    // find the time between the last two beats
  
  float currentBPM = 60000 / diff;    // convert to beats per minute
  cBPM = (int)currentBPM;  
  beats[beatIndex] = currentBPM;  // store to array to convert the average
  float total = 0.0;
  for (int i = 0; i < 500; i++){
    total += beats[i];
  }
  BPM = int(total / 500);
  beat_old = beat_new;
  beatIndex = (beatIndex + 1) % 500;  // cycle through the array instead of using FIFO queue
}
